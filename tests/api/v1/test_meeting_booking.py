"""Public widget endpoints for meeting booking — Stage 3 Slice 3 tests.

Fixtures: pg_client + db_session (Postgres-backed, rolled-back per test).
These are the real fixture names defined in tests/conftest.py; the brief used
the placeholder name ``client_db`` which does not exist in this repo.
"""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.db.models.lead import Lead
from app.db.models.sales_agent_config import TenantSalesAgentConfig
from app.db.models.tenant import Tenant


def _seed(db, *, booking_enabled=True):
    """Create a Tenant + TenantSalesAgentConfig + Lead; return (tenant, conv_id, lead)."""
    u = uuid4().hex[:8]
    t = Tenant(
        name=f"t_{u}",
        display_name=f"T {u}",
        api_key_hash=uuid4().hex,
        subscription_tier="trial",
        subscription_status="trialing",
    )
    db.add(t)
    db.flush()

    cfg = TenantSalesAgentConfig.default_for(t.id)
    cfg.meeting_booking_enabled = booking_enabled
    # All-week, all-day availability with no buffer so tests always find slots.
    cfg.availability = {
        "weekdays": [1, 2, 3, 4, 5, 6, 7],
        "start": "00:00",
        "end": "23:30",
        "slot_minutes": 30,
        "buffer_minutes": 0,
        "tenant_timezone": "UTC",
    }
    db.add(cfg)

    conv = uuid4()
    lead = Lead(tenant_id=t.id, conversation_id=conv, email="a@b.com")
    db.add(lead)
    db.commit()
    return t, conv, lead


def test_availability_returns_slots(pg_client, db_session):
    """GET /availability → 200 with non-empty slots when booking is enabled."""
    t, conv, lead = _seed(db_session)
    r = pg_client.get(
        f"/api/v1/chat/meetings/availability?conversation_id={conv}&days=2"
    )
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert len(body["data"]["slots"]) > 0
    assert "start_utc" in body["data"]["slots"][0]


def test_availability_disabled_returns_empty(pg_client, db_session):
    """GET /availability → 200 with empty slots when booking is disabled."""
    t, conv, lead = _seed(db_session, booking_enabled=False)
    r = pg_client.get(
        f"/api/v1/chat/meetings/availability?conversation_id={conv}"
    )
    assert r.status_code == 200
    assert r.json()["data"]["slots"] == []


def test_book_creates_and_confirms(pg_client, db_session):
    """POST /chat/meetings → 200 with booked meeting details."""
    t, conv, lead = _seed(db_session)
    when = (
        datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        + timedelta(days=1)
    )
    r = pg_client.post(
        "/api/v1/chat/meetings",
        json={
            "conversation_id": str(conv),
            "scheduled_at": when.isoformat(),
            "visitor_timezone": "Africa/Lagos",
            "use_case": "Demo",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["data"]["meeting"]["duration_min"] == 30


def test_double_book_conflicts(pg_client, db_session):
    """Second booking for the same tenant + slot → 409."""
    t, conv, lead = _seed(db_session)
    when = (
        datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        + timedelta(days=1)
    )
    payload = {
        "conversation_id": str(conv),
        "scheduled_at": when.isoformat(),
        "visitor_timezone": "UTC",
    }
    # First booking succeeds.
    r1 = pg_client.post("/api/v1/chat/meetings", json=payload)
    assert r1.status_code == 200

    # A second lead / conversation for the same tenant tries the same slot.
    conv2 = uuid4()
    db_session.add(Lead(tenant_id=t.id, conversation_id=conv2, email="c@d.com"))
    db_session.commit()

    payload2 = {**payload, "conversation_id": str(conv2)}
    r2 = pg_client.post("/api/v1/chat/meetings", json=payload2)
    assert r2.status_code == 409
