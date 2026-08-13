"""Regression tests for VisitAnalyticsService.record_visit idempotency.

record_visit previously used a check-then-insert (SELECT, then INSERT when
absent). Two concurrent first-visits for the same daily bucket both saw "no row"
and both INSERTed, and the second commit violated
``uq_daily_visits_per_visitor_day`` -> HTTP 500. The fix uses an atomic
``INSERT ... ON CONFLICT DO UPDATE`` upsert.

Both call sites (dashboard + chat widget) pass a non-null ``tenant_id``, which is
what the unique constraint (and therefore ON CONFLICT) keys on — so these tests
use a real tenant, matching the production failure.

Postgres-only (upsert + unique constraint), so they use the ``db_session`` /
``pg_engine`` fixtures like the other Postgres-backed tests in this suite.
"""

import threading
import time
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy.orm import sessionmaker

from app.db.models.tenant import Tenant
from app.db.models.visit_analytics import DailyVisit
from app.services.visit_analytics_service import VisitAnalyticsService


def _make_tenant(session) -> Tenant:
    prefix = uuid4().hex[:8]
    tenant = Tenant(
        name=f"tenant_{prefix}",
        display_name=f"Tenant {prefix}",
        api_key_hash=uuid4().hex,
        subscription_tier="trial",
        subscription_status="trialing",
    )
    session.add(tenant)
    session.flush()
    return tenant


def test_record_visit_repeat_increments_single_row(db_session):
    """Repeat visits for the same bucket increment the counter in place."""
    tenant = _make_tenant(db_session)
    key = f"user:{uuid4().hex}"
    svc = VisitAnalyticsService(db_session)

    svc.record_visit(
        source="dashboard", visitor_key=key, tenant_id=tenant.id, meta={"page_path": "/dashboard"}
    )
    svc.record_visit(
        source="dashboard",
        visitor_key=key,
        tenant_id=tenant.id,
        meta={"page_path": "/dashboard/settings"},
    )

    rows = db_session.query(DailyVisit).filter_by(tenant_id=tenant.id, visitor_key=key).all()
    assert len(rows) == 1
    assert rows[0].visit_count == 2
    # meta is JSON (no SQL merge): the latest event's meta is kept.
    assert rows[0].meta == {"page_path": "/dashboard/settings"}

    # A repeat visit without meta must not clobber the stored meta.
    svc.record_visit(source="dashboard", visitor_key=key, tenant_id=tenant.id)
    db_session.refresh(rows[0])
    assert rows[0].visit_count == 3
    assert rows[0].meta == {"page_path": "/dashboard/settings"}


def test_record_visit_race_does_not_500(pg_engine):
    """A concurrent insert of the same bucket must not make record_visit 500.

    Deterministic reproduction: a ``holder`` session INSERTs the bucket row and
    holds the unique-key lock WITHOUT committing; a racer thread calls
    record_visit, whose own INSERT blocks on that lock; the holder then commits,
    materializing the row underneath the racer. Old check-then-insert -> the
    racer's INSERT resolves into a duplicate -> UniqueViolation (500). The upsert
    -> the racer's ON CONFLICT DO UPDATE increments instead, no error.
    """
    maker = sessionmaker(bind=pg_engine)
    key = f"user:{uuid4().hex}"

    setup = maker()
    tenant = _make_tenant(setup)
    setup.commit()
    tenant_id = tenant.id
    setup.close()

    now = datetime.now(UTC)
    holder = maker()
    # INSERT + flush acquires the unique-key lock; do NOT commit yet.
    holder.add(
        DailyVisit(
            id=uuid4(),
            visit_date=now.date(),
            tenant_id=tenant_id,
            source="dashboard",
            visitor_key=key,
            visit_count=1,
            first_seen_at=now,
            last_seen_at=now,
            meta={},
        )
    )
    holder.flush()

    result: dict = {}
    started = threading.Event()

    def racer() -> None:
        session = maker()
        try:
            started.set()
            VisitAnalyticsService(session).record_visit(
                source="dashboard", visitor_key=key, tenant_id=tenant_id
            )
            result["ok"] = True
        except Exception as exc:  # noqa: BLE001
            result["error"] = exc
        finally:
            session.close()

    thread = threading.Thread(target=racer)
    thread.start()
    started.wait(timeout=5)
    time.sleep(0.5)  # let the racer's INSERT reach — and block on — the lock
    holder.commit()  # release the lock; the racer now resolves against a real row
    holder.close()
    thread.join(timeout=10)

    verify = maker()
    try:
        rows = verify.query(DailyVisit).filter_by(tenant_id=tenant_id, visitor_key=key).all()
        assert "error" not in result, f"record_visit raised under race: {result.get('error')!r}"
        assert result.get("ok") is True
        assert len(rows) == 1
        assert rows[0].visit_count == 2
    finally:
        # Deleting the tenant cascades to daily_visits (ondelete=CASCADE).
        verify.query(Tenant).filter_by(id=tenant_id).delete()
        verify.commit()
        verify.close()
