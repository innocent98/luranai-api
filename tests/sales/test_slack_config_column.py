from uuid import uuid4
from app.db.models.sales_agent_config import TenantSalesAgentConfig
from app.db.models.tenant import Tenant


def _tenant(db):
    u = uuid4().hex[:8]
    t = Tenant(name=f"t_{u}", display_name="T", api_key_hash=uuid4().hex,
               subscription_tier="trial", subscription_status="trialing")
    db.add(t); db.flush(); db.refresh(t)
    return t


def test_slack_webhook_url_persists_and_defaults_none(db_session):
    t = _tenant(db_session)
    cfg = TenantSalesAgentConfig.default_for(t.id)
    assert cfg.slack_webhook_url is None
    cfg.slack_webhook_url = "https://hooks.slack.com/services/T/B/x"
    db_session.add(cfg); db_session.flush(); db_session.refresh(cfg)
    assert cfg.slack_webhook_url == "https://hooks.slack.com/services/T/B/x"
