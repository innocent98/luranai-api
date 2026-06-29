"""Notification trigger integration tests (Stage 3 Slice 5a).

Verifies that each of the three trigger seams (meeting_booked, quote_generated,
sales_ready_lead) writes exactly the right number of NotificationLog rows, and
that the sales_ready trigger is deduplicated when the lead stays at or above the
threshold on subsequent turns.

Enqueue helpers are patched to no-ops so no Redis connection is required.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.notification_log import NotifEvent, NotificationLog
from app.db.models.sales_agent_config import TenantSalesAgentConfig

# ---------------------------------------------------------------------------
# Re-use seed/patch helpers from existing Slice test modules
# ---------------------------------------------------------------------------
from tests.api.v1.test_chat_lead_capture import (
    _automation_patch,
    _make_tenant,
    _retrieval_patch,
    _sales_llm_patch,
)
from tests.api.v1.test_chat_quote import (
    _CAPTURED_WITH_TEAM_SIZE,
    _OFFER_QUOTE_SIGNALS,
    _make_quote_tenant,
)
from tests.api.v1.test_meeting_booking import _seed as _meeting_seed

# ---------------------------------------------------------------------------
# Shared noop enqueue stubs
# ---------------------------------------------------------------------------


def _noop_sync(jobs):
    pass


async def _noop_async(jobs):
    pass


# ---------------------------------------------------------------------------
# Signal/captured data that drives a lead score from 0 → ≥86 (sales_ready).
#
# Score breakdown:
#   provided_email   (email field)  +20
#   company_name     (company field) +10
#   team_over_50     (team_size=60)  +15
#   asked_features   (signal)        +10
#   requested_demo   (signal)        +15
#   ready_to_buy     (signal)        +25
#   ─────────────────────────────────────
#   total                            95  ≥ 86 ✓
# ---------------------------------------------------------------------------

_HIGH_INTENT_SIGNALS = {
    "asked_features": True,
    "requested_demo": True,
    "mentioned_competitor": False,
    "ready_to_buy": True,
    "offer_meeting": False,
    "offer_quote": False,
}

_HIGH_INTENT_CAPTURED = {
    "name": "Bob",
    "email": "bob@corp.com",
    "company": "Acme Corp",
    "team_size": "60",
    "industry": None,
}


# ---------------------------------------------------------------------------
# Helper: create a sales-ready-capable tenant with email notifications on
# ---------------------------------------------------------------------------


def _make_sales_ready_tenant(db: Session) -> "UUID":  # type: ignore[name-defined]
    """Create a sales-enabled tenant and set notification_email so dispatch resolves."""
    tenant_id = _make_tenant(db, sales_enabled=True)
    cfg = db.execute(
        select(TenantSalesAgentConfig).where(TenantSalesAgentConfig.tenant_id == tenant_id)
    ).scalar_one()
    cfg.notification_email = "sales@example.com"
    cfg.notify_channels = {"email": True, "slack": False, "in_app": False}
    db.commit()
    return tenant_id


# ===========================================================================
# 1. meeting_booked → 2 NotificationLog rows (rep + visitor)
# ===========================================================================


def test_meeting_booked_writes_two_logs(pg_client, db_session):
    """Booking a meeting with email channel on → 2 NotificationLog rows.

    The rep log goes to notification_email; the visitor log goes to the lead
    email that was set in _seed ("a@b.com").
    """
    t, conv, lead = _meeting_seed(db_session)

    # Provide a named notification_email so _rep_recipient resolves without a User.
    cfg = db_session.execute(
        select(TenantSalesAgentConfig).where(TenantSalesAgentConfig.tenant_id == t.id)
    ).scalar_one()
    cfg.notification_email = "rep@example.com"
    db_session.commit()

    when = (
        datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        + timedelta(days=1)
    )

    with patch("app.services.notification_enqueue.enqueue_jobs_sync", _noop_sync):
        r = pg_client.post(
            "/api/v1/chat/meetings",
            json={
                "conversation_id": str(conv),
                "scheduled_at": when.isoformat(),
                "visitor_timezone": "UTC",
                "use_case": "Demo",
            },
        )

    assert r.status_code == 200, r.text

    logs = db_session.execute(
        select(NotificationLog).where(
            NotificationLog.tenant_id == t.id,
            NotificationLog.event == NotifEvent.MEETING_BOOKED,
        )
    ).scalars().all()

    assert len(logs) == 2, f"Expected 2 NotificationLog rows (rep + visitor), got {len(logs)}"
    recipients = {log.recipient for log in logs}
    assert "rep@example.com" in recipients, "Rep email missing from logs"
    assert "a@b.com" in recipients, "Visitor (lead) email missing from logs"


# ===========================================================================
# 2. quote_generated → 1 NotificationLog row (rep alert)
# ===========================================================================


def test_quote_generated_writes_one_log(client, db: Session):
    """Chat turn that generates a quote → 1 QUOTE_GENERATED log for the rep."""
    tenant_id = _make_quote_tenant(db, quotation_enabled=True)

    # Set notification_email so _rep_recipient resolves (no User created in fixture).
    cfg = db.execute(
        select(TenantSalesAgentConfig).where(TenantSalesAgentConfig.tenant_id == tenant_id)
    ).scalar_one()
    cfg.notification_email = "sales@example.com"
    db.commit()

    with (
        _sales_llm_patch("PRICING_INQUIRY", _CAPTURED_WITH_TEAM_SIZE, _OFFER_QUOTE_SIGNALS),
        _retrieval_patch(),
        _automation_patch(),
        patch("app.services.notification_enqueue.enqueue_jobs_async", _noop_async),
    ):
        r = client.post(
            "/api/v1/chat/message",
            json={
                "message": "What will Growth cost for 58 users?",
                "tenant_id": str(tenant_id),
                "user_identifier": "nv1",
            },
        )

    assert r.status_code == 200, r.text

    logs = db.execute(
        select(NotificationLog).where(
            NotificationLog.tenant_id == tenant_id,
            NotificationLog.event == NotifEvent.QUOTE_GENERATED,
        )
    ).scalars().all()

    assert len(logs) == 1, f"Expected 1 QUOTE_GENERATED log, got {len(logs)}"
    assert logs[0].entity_type == "quote"
    assert logs[0].recipient == "sales@example.com"


# ===========================================================================
# 3a. sales_ready_lead → fires on first threshold crossing
# ===========================================================================


def test_sales_ready_fires_on_crossing(client, db: Session):
    """Lead crossing from <86 to ≥86 → exactly 1 SALES_READY_LEAD log written."""
    tenant_id = _make_sales_ready_tenant(db)

    # Turn 1: no captured data → score stays at 0.
    with (
        _sales_llm_patch("PRICING_INQUIRY", {}, {}),
        _retrieval_patch(),
        _automation_patch(),
        patch("app.services.notification_enqueue.enqueue_jobs_async", _noop_async),
    ):
        r1 = client.post(
            "/api/v1/chat/message",
            json={
                "message": "hello",
                "tenant_id": str(tenant_id),
                "user_identifier": "sr1",
            },
        )
    assert r1.status_code == 200, r1.text
    sid = r1.json()["session_id"]

    # No logs yet — threshold not yet crossed.
    early_logs = db.execute(
        select(NotificationLog).where(
            NotificationLog.tenant_id == tenant_id,
            NotificationLog.event == NotifEvent.SALES_READY_LEAD,
        )
    ).scalars().all()
    assert len(early_logs) == 0

    # Turn 2: high-intent signals drive score to 95 ≥ 86.
    with (
        _sales_llm_patch("ENTERPRISE_PROSPECT", _HIGH_INTENT_CAPTURED, _HIGH_INTENT_SIGNALS),
        _retrieval_patch(),
        _automation_patch(),
        patch("app.services.notification_enqueue.enqueue_jobs_async", _noop_async),
    ):
        r2 = client.post(
            "/api/v1/chat/message",
            json={
                "message": "We're a 60-person company ready to buy",
                "session_id": sid,
                "tenant_id": str(tenant_id),
                "user_identifier": "sr1",
            },
        )
    assert r2.status_code == 200, r2.text

    logs = db.execute(
        select(NotificationLog).where(
            NotificationLog.tenant_id == tenant_id,
            NotificationLog.event == NotifEvent.SALES_READY_LEAD,
        )
    ).scalars().all()
    assert len(logs) == 1, (
        f"Expected 1 SALES_READY_LEAD log after threshold crossing, got {len(logs)}"
    )
    assert logs[0].entity_type == "lead"
    assert logs[0].recipient == "sales@example.com"


# ===========================================================================
# 3b. sales_ready_lead → deduplicated when lead stays ≥86
# ===========================================================================


def test_sales_ready_deduplicated_on_subsequent_turn(client, db: Session):
    """Lead already ≥86 and stays ≥86 on next turn → still only 1 log (no re-fire)."""
    tenant_id = _make_sales_ready_tenant(db)

    # Turn 1: anonymous lead, score = 0.
    with (
        _sales_llm_patch("PRICING_INQUIRY", {}, {}),
        _retrieval_patch(),
        _automation_patch(),
        patch("app.services.notification_enqueue.enqueue_jobs_async", _noop_async),
    ):
        r1 = client.post(
            "/api/v1/chat/message",
            json={
                "message": "hi",
                "tenant_id": str(tenant_id),
                "user_identifier": "dedup1",
            },
        )
    sid = r1.json()["session_id"]

    # Turn 2: cross threshold → 1 log.
    with (
        _sales_llm_patch("ENTERPRISE_PROSPECT", _HIGH_INTENT_CAPTURED, _HIGH_INTENT_SIGNALS),
        _retrieval_patch(),
        _automation_patch(),
        patch("app.services.notification_enqueue.enqueue_jobs_async", _noop_async),
    ):
        client.post(
            "/api/v1/chat/message",
            json={
                "message": "60-person company ready to buy",
                "session_id": sid,
                "tenant_id": str(tenant_id),
                "user_identifier": "dedup1",
            },
        )

    # Turn 3: still ≥86 — crossed_sales_ready returns False → no new log.
    with (
        _sales_llm_patch("ENTERPRISE_PROSPECT", _HIGH_INTENT_CAPTURED, _HIGH_INTENT_SIGNALS),
        _retrieval_patch(),
        _automation_patch(),
        patch("app.services.notification_enqueue.enqueue_jobs_async", _noop_async),
    ):
        client.post(
            "/api/v1/chat/message",
            json={
                "message": "still very interested",
                "session_id": sid,
                "tenant_id": str(tenant_id),
                "user_identifier": "dedup1",
            },
        )

    logs = db.execute(
        select(NotificationLog).where(
            NotificationLog.tenant_id == tenant_id,
            NotificationLog.event == NotifEvent.SALES_READY_LEAD,
        )
    ).scalars().all()
    assert len(logs) == 1, (
        f"Expected exactly 1 (deduped) SALES_READY_LEAD log, got {len(logs)}"
    )
