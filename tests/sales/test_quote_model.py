from uuid import uuid4

from app.db.models.quote import Quote, QuoteStatus
from app.db.models.sales_agent_config import TenantSalesAgentConfig
from app.db.models.tenant import Tenant
from app.db.models.lead import Lead


def _tenant(db):
    u = uuid4().hex[:8]
    t = Tenant(name=f"t_{u}", display_name=f"T {u}", api_key_hash=uuid4().hex,
               subscription_tier="trial", subscription_status="trialing")
    db.add(t); db.flush(); db.refresh(t)
    return t


def test_quote_persists_lowercase_status(db_session):
    t = _tenant(db_session)
    lead = Lead(tenant_id=t.id, conversation_id=uuid4(), email="a@b.com")
    db_session.add(lead); db_session.flush()
    q = Quote(tenant_id=t.id, lead_id=lead.id, conversation_id=lead.conversation_id,
              currency="NGN", line_items=[{"label": "Growth", "detail": "11–50 users", "amount": 45000}],
              total=45000, status=QuoteStatus.DRAFT)
    db_session.add(q); db_session.flush(); db_session.refresh(q)
    assert q.id is not None and q.total == 45000
    assert QuoteStatus.DRAFT.value == "draft"


def test_config_annual_discount_default(db_session):
    t = _tenant(db_session)
    cfg = TenantSalesAgentConfig.default_for(t.id)
    assert cfg.annual_discount_pct == 0
    assert cfg.pricing_tiers == []
