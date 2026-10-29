import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

# Set encryption key BEFORE any app imports so SQLAlchemy models with
# EncryptedString columns can encrypt/decrypt during tests.
if not os.environ.get("ENCRYPTION_KEY"):
    from cryptography.fernet import Fernet

    os.environ["ENCRYPTION_KEY"] = Fernet.generate_key().decode()
    os.environ.setdefault("ENCRYPTION_MIGRATION_MODE", "true")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.session import get_db
from app.main import app

# ---------------------------------------------------------------------------
# SQLite fixtures (existing — must not change)
# ---------------------------------------------------------------------------

# Test database - use in-memory SQLite for tests
SQLALCHEMY_DATABASE_URL = "sqlite:///:memory:"
engine = create_engine(
    SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False}, echo=False
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture(scope="session", autouse=True)
def setup_test_database():
    """Create all tables once for the entire test session."""
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture(scope="function")
def db(setup_test_database):
    """Provide a database session for each test."""
    connection = engine.connect()
    transaction = connection.begin()
    db = TestingSessionLocal(bind=connection)

    try:
        yield db
    finally:
        db.close()
        transaction.rollback()
        connection.close()


@pytest.fixture(scope="function")
def client(db):
    def override_get_db():
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture(scope="function", autouse=True)
def _reset_http_rate_limits():
    """Clear HTTP rate-limit counters before each test.

    Every HTTP rate limiter keys on the caller IP, and TestClient uses a single
    fixed client host, so counters accumulate across the full-suite run and push
    later tests over the limit (e.g. registration is 3/hour in the ``test`` env,
    but the suite issues several registrations). Clearing the rate-limit keys
    before each test isolates them, while still letting a single test build up to
    its own 429 assertion (e.g. the public-auditor limit tests).

    Only rate-limit keys are removed (not a full FLUSHDB) so unrelated Redis
    state is left intact. Fails open if Redis is unavailable so suites without a
    Redis server still run.
    """
    try:
        import redis as _redis

        from app.core.config import settings as _settings

        _client = _redis.Redis.from_url(_settings.REDIS_URL)
        for pattern in ("http_*", "public_audit*", "openai_*_api*"):
            for key in _client.scan_iter(match=pattern, count=500):
                _client.delete(key)
        _client.close()
    except Exception:
        pass
    yield


# ---------------------------------------------------------------------------
# Postgres-backed fixtures for promotional-grants tests
# ---------------------------------------------------------------------------

PG_DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://luran_user:luran_password_dev@localhost:5432/luran_db",
)


@pytest.fixture(scope="session")
def pg_engine():
    """Session-scoped Postgres engine with schema + baseline seed data.

    `Base.metadata.create_all` is idempotent — on local dev it's a no-op
    (tables exist via migrations); in CI it bootstraps the empty test DB.

    Subscription plans are seeded once per session because several services
    (PromotionalGrantService, PlanService) resolve tier -> plan at runtime
    and crash with `ValueError: Subscription plan 'trial' not found` against
    an empty plans table. `create_subscription_plans` is the same function
    `scripts/seed_data.py` uses, so test data matches dev/prod exactly and
    is upsert-style (skips rows that already exist).
    """
    from scripts.seed_data import create_subscription_plans

    engine = create_engine(PG_DATABASE_URL)
    Base.metadata.create_all(bind=engine)

    # Partial / expression indexes from migration 0c1be6a3a289 — not declared
    # on the model, so create_all skips them. Tests assert IntegrityError on
    # these uniqueness constraints, so they must exist. All `IF NOT EXISTS`
    # so this is idempotent against a migration-managed dev DB.
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS promo_grants_one_active_per_tenant "
                "ON promotional_grants (tenant_id) WHERE status = 'active'"
            )
        )
        conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS promo_grants_idempotency "
                "ON promotional_grants (tenant_id, campaign_code) "
                "WHERE status IN ('active', 'converted')"
            )
        )
        conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS auto_signup_grant_config_singleton "
                "ON auto_signup_grant_config ((true))"
            )
        )
        # Singleton row also lives in migration, not create_all. The migration
        # sets server_default=gen_random_uuid() on id; create_all uses the
        # model's Python-side default so we must supply id explicitly here.
        conn.execute(
            text(
                "INSERT INTO auto_signup_grant_config (id, enabled) "
                "VALUES (gen_random_uuid(), false) ON CONFLICT DO NOTHING"
            )
        )

    seed_session = sessionmaker(bind=engine)()
    try:
        create_subscription_plans(seed_session)
        seed_session.commit()
    finally:
        seed_session.close()

    yield engine
    engine.dispose()


@pytest.fixture(scope="function")
def db_session(pg_engine):
    """Postgres session using external-transaction + savepoint rollback pattern."""
    connection = pg_engine.connect()
    transaction = connection.begin()
    session_factory = sessionmaker(bind=connection)
    session = session_factory()
    session.begin_nested()  # SAVEPOINT

    @event.listens_for(session, "after_transaction_end")
    def restart_savepoint(sess, trans):
        if trans.nested and not trans._parent.nested:
            try:
                sess.begin_nested()
            except InvalidRequestError:
                # A savepoint opened with ``with session.begin_nested():`` is closing right now;
                # a new one cannot start inside that context manager. Skipping the restart is
                # safe: the outer connection transaction is still rolled back at teardown.
                pass

    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture(scope="function")
def super_admin_user(db_session):
    """Insert a SUPER_ADMIN PlatformAdmin row; roll back after each test."""
    from passlib.context import CryptContext

    from app.db.models.platform_admin import PlatformAdmin, PlatformAdminRole

    pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
    unique_prefix = uuid4().hex[:8]
    email = f"{unique_prefix}@testdomain.com"

    admin = PlatformAdmin(
        email=email,
        hashed_password=pwd_context.hash("test"),
        full_name=f"Test Admin {unique_prefix}",
        role=PlatformAdminRole.SUPER_ADMIN,
        is_active=True,
    )
    db_session.add(admin)
    db_session.flush()
    db_session.refresh(admin)
    return admin


@pytest.fixture(scope="function")
def super_admin_token(super_admin_user):
    """JWT for the super_admin_user, carrying the claims verify_token_data needs."""
    from app.core.security import create_access_token

    token = create_access_token(
        subject=str(super_admin_user.id),
        additional_claims={
            "role": super_admin_user.role.value,
            "admin_type": "platform_admin",
        },
    )
    return token


@pytest.fixture(scope="function")
def regular_user_token(db_session):
    """JWT for a regular tenant user — used to verify endpoints reject non-admin auth."""
    from app.core.security import create_access_token
    from app.db.models.tenant import Tenant
    from app.db.models.user import User, UserRole

    unique_prefix = uuid4().hex[:8]

    tenant = Tenant(
        name=f"tenant_{unique_prefix}",
        display_name=f"Tenant {unique_prefix}",
        api_key_hash=uuid4().hex,
        subscription_tier="trial",
        subscription_status="trialing",
    )
    db_session.add(tenant)
    db_session.flush()

    user = User(
        email=f"user_{unique_prefix}@example.com",
        hashed_password="$2b$12$placeholder_hash_not_real",
        full_name=f"Regular User {unique_prefix}",
        role=UserRole.TENANT_ADMIN,
        tenant_id=tenant.id,
        is_active=True,
    )
    db_session.add(user)
    db_session.flush()
    db_session.refresh(user)

    token = create_access_token(
        subject=str(user.id),
        additional_claims={
            "role": user.role.value,
            "tenant_id": str(user.tenant_id),
        },
    )
    return token


@pytest.fixture(scope="function")
def pg_client(db_session):
    """TestClient whose get_db override returns the Postgres db_session."""

    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture(scope="function")
def churned_tenants_factory(db_session):
    """Factory that creates N Tenant rows with specified churn status."""
    from app.db.models.tenant import Tenant, TenantCapabilities

    def factory(count: int = 1, status: str = "trial_expired", churned_days_ago: int = 30):
        tenants = []
        for _ in range(count):
            unique_prefix = uuid4().hex[:8]
            tenant = Tenant(
                name=f"churned_{unique_prefix}",
                display_name=f"Churned Tenant {unique_prefix}",
                api_key_hash=uuid4().hex,
                subscription_tier="trial",
                subscription_status=status,
                trial_ends_at=datetime.now(UTC) - timedelta(days=churned_days_ago),
            )
            db_session.add(tenant)
            tenants.append(tenant)
        db_session.flush()
        for t in tenants:
            db_session.refresh(t)
            capabilities = TenantCapabilities(tenant_id=t.id)
            db_session.add(capabilities)
        db_session.commit()
        for t in tenants:
            db_session.refresh(t)
        return tenants

    return factory


def _bare_voice_tenant(db_session):
    """Create a minimal, valid `Tenant` row (no `TenantCapabilities`).

    Shared by `voice_tenant_factory` below and by
    `tests/voice/test_voice_capabilities.py::_tenant`, which imports this
    directly rather than keeping its own copy — both need the same
    genuinely-required `Tenant` columns (`name`, `display_name`,
    `api_key_hash`, `subscription_tier`, `subscription_status`; `Tenant` has
    no `slug` column, so a factory built from that field fails at insert
    time).
    """
    from app.db.models.tenant import Tenant

    unique_prefix = uuid4().hex[:8]
    tenant = Tenant(
        name=f"voice_test_{unique_prefix}",
        display_name=f"Voice Test {unique_prefix}",
        api_key_hash=uuid4().hex,
        subscription_tier="trial",
        subscription_status="trialing",
    )
    db_session.add(tenant)
    db_session.flush()
    db_session.refresh(tenant)
    return tenant


@pytest.fixture(scope="function")
def voice_tenant_factory(db_session):
    """Create a tenant with specific voice capabilities."""
    from app.db.models.tenant import TenantCapabilities

    def _make(*, can_use_voice: bool, max_voice_minutes_per_month: int | None):
        tenant = _bare_voice_tenant(db_session)
        db_session.add(
            TenantCapabilities(
                tenant_id=tenant.id,
                can_use_voice=can_use_voice,
                max_voice_minutes_per_month=max_voice_minutes_per_month,
            )
        )
        db_session.commit()
        db_session.refresh(tenant)
        return tenant

    return _make


@pytest.fixture(scope="function")
def voice_usage_setter():
    """Seed the Redis monthly voice-minute counter for a tenant.

    Synchronous on purpose so it is usable from both async tests and sync
    TestClient tests. Never use `asyncio.get_event_loop()` — it is deprecated
    and errors on Python 3.12+ when no loop is running.

    `asyncio.run()` alone is not enough: our own async voice tests (e.g.
    `test_minute_cap_reached_denies`) already have a pytest-asyncio event
    loop running for the duration of the test body, and `asyncio.run()`
    raises `RuntimeError: asyncio.run() cannot be called from a running
    event loop` if called from inside one. When a loop is already running,
    fall back to a dedicated thread with its own fresh loop instead.
    """
    import asyncio
    import threading

    from app.services.voice.metering import VoiceMeter

    def _set(tenant_id, *, minutes: int):
        coro = VoiceMeter().add_minutes(tenant_id, minutes)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(coro)
            return

        thread = threading.Thread(target=asyncio.run, args=(coro,))
        thread.start()
        thread.join()

    return _set


@pytest.fixture(scope="function")
def voice_session_factory(db_session):
    """Create a ConversationSession belonging to a tenant."""
    from app.db.models.conversation import ConversationSession, SessionStatus

    def _make(tenant_id):
        session = ConversationSession(
            tenant_id=tenant_id,
            user_identifier=f"visitor-{uuid4().hex[:8]}",
            status=SessionStatus.ACTIVE,
        )
        db_session.add(session)
        db_session.flush()
        session.user_identifier = f"visitor-{session.id}"  # voice mint needs the credential
        db_session.commit()
        return session

    return _make


@pytest.fixture(scope="function")
def minted_voice_token():
    """Mint a real voice token so endpoint tests exercise the true resolve path."""
    import asyncio

    from app.services.voice.session_tokens import VoiceSessionTokenStore

    def _make(tenant_id, session_id, *, text_out: bool = False):
        store = VoiceSessionTokenStore()
        return asyncio.run(
            store.mint(tenant_id=tenant_id, luran_session_id=session_id, text_out=text_out)
        )

    return _make
