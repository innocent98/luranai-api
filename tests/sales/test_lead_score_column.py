"""TDD: Lead.score_breakdown — nullable JSON column persists frozen scoring breakdown."""

from uuid import uuid4

from app.db.models.lead import Lead
from app.db.models.tenant import Tenant


def test_lead_persists_score_breakdown(db_session):
    unique = uuid4().hex[:8]
    tenant = Tenant(
        name=f"tenant_{unique}",
        display_name=f"Tenant {unique}",
        api_key_hash=uuid4().hex,
        subscription_tier="trial",
        subscription_status="trialing",
    )
    db_session.add(tenant)
    db_session.flush()
    db_session.refresh(tenant)

    lead = Lead(
        tenant_id=tenant.id,
        email="a@b.com",
        lead_score=70,
        score_breakdown=[{"key": "provided_email", "weight": 20, "fired": True}],
    )
    db_session.add(lead)
    db_session.commit()
    db_session.refresh(lead)
    assert lead.score_breakdown[0]["key"] == "provided_email"
