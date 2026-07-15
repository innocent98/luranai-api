from uuid import uuid4
from datetime import datetime, timezone

from app.db.models.lead import Lead
from app.db.models.meeting import Meeting, MeetingStatus
from app.db.models.sales_agent_config import TenantSalesAgentConfig
from app.db.models.tenant import Tenant


def _tenant(db):
    u = uuid4().hex[:8]
    t = Tenant(name=f"t_{u}", display_name=f"T {u}", api_key_hash=uuid4().hex,
               subscription_tier="trial", subscription_status="trialing")
    db.add(t); db.flush(); db.refresh(t)
    return t


def _lead(db, tenant_id):
    lead = Lead(tenant_id=tenant_id)
    db.add(lead); db.flush(); db.refresh(lead)
    return lead


def test_meeting_persists_with_lowercase_status(db_session):
    t = _tenant(db_session)
    lead = _lead(db_session, t.id)
    m = Meeting(
        tenant_id=t.id, lead_id=lead.id, conversation_id=uuid4(),
        scheduled_at=datetime(2026, 7, 7, 13, 0, tzinfo=timezone.utc),
        duration_min=30, visitor_timezone="Africa/Lagos",
        status=MeetingStatus.SCHEDULED,
    )
    db_session.add(m); db_session.flush(); db_session.refresh(m)
    assert m.id is not None
    assert m.status == MeetingStatus.SCHEDULED
    assert MeetingStatus.SCHEDULED.value == "scheduled"


def test_config_availability_default(db_session):
    t = _tenant(db_session)
    cfg = TenantSalesAgentConfig.default_for(t.id)
    assert cfg.availability["slot_minutes"] == 30
    assert cfg.availability["weekdays"] == [1, 2, 3, 4, 5]
    assert cfg.availability["tenant_timezone"] == "UTC"
