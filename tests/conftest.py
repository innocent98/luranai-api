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
from sqlalchemy import create_engine, event
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


# ---------------------------------------------------------------------------
# Postgres-backed fixtures for promotional-grants tests
# ---------------------------------------------------------------------------

PG_DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://luran_user:luran_password_dev@localhost:5432/luran_db",
)


@pytest.fixture(scope="function")
def db_session():
    """Postgres session using external-transaction + savepoint rollback pattern."""
    pg_engine = create_engine(PG_DATABASE_URL)
    connection = pg_engine.connect()
    transaction = connection.begin()
    session_factory = sessionmaker(bind=connection)
    session = session_factory()
    session.begin_nested()  # SAVEPOINT

    @event.listens_for(session, "after_transaction_end")
    def restart_savepoint(sess, trans):
        if trans.nested and not trans._parent.nested:
            sess.begin_nested()

    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()
        pg_engine.dispose()


@pytest.fixture(scope="function")
def super_admin_user(db_session):
    """Insert a SUPER_ADMIN PlatformAdmin row; roll back after each test."""
    from passlib.context import CryptContext

    from app.db.models.platform_admin import PlatformAdmin, PlatformAdminRole

    pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
    unique_prefix = uuid4().hex[:8]
    email = f"{unique_prefix}@test.local"

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
        email=f"user_{unique_prefix}@test.local",
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
    from app.db.models.tenant import Tenant

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
        return tenants

    return factory
