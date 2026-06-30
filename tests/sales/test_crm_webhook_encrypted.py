"""crm_webhook_url is encrypted at rest and decrypts transparently (Slice 5e Task 1)."""
from uuid import uuid4

from sqlalchemy import text

from app.db.models.tenant import Tenant
from app.db.models.sales_agent_config import TenantSalesAgentConfig


def _tenant(db):
    t = Tenant(name=f"t_{uuid4().hex[:8]}", display_name="Acme", api_key_hash=uuid4().hex,
               subscription_tier="trial", subscription_status="trialing")
    db.add(t); db.flush()
    return t


def test_crm_webhook_url_roundtrips_plaintext(db_session):
    t = _tenant(db_session)
    cfg = TenantSalesAgentConfig.default_for(t.id)
    cfg.crm_webhook_url = "https://hooks.zapier.com/hooks/catch/123/abc?token=secret"
    db_session.add(cfg); db_session.commit()
    db_session.expire_all()
    reloaded = db_session.get(TenantSalesAgentConfig, cfg.id)
    assert reloaded.crm_webhook_url == "https://hooks.zapier.com/hooks/catch/123/abc?token=secret"


def test_crm_webhook_url_ciphertext_in_raw_db(db_session):
    t = _tenant(db_session)
    cfg = TenantSalesAgentConfig.default_for(t.id)
    cfg.crm_webhook_url = "https://hooks.zapier.com/PLAINTEXT-MARKER"
    db_session.add(cfg); db_session.commit()
    raw = db_session.execute(
        text("SELECT crm_webhook_url FROM tenant_sales_agent_config WHERE id = :id"),
        {"id": str(cfg.id)},
    ).scalar_one()
    assert raw is not None
    assert "PLAINTEXT-MARKER" not in raw  # stored as Fernet ciphertext


def test_default_for_sets_crm_webhook_url_none_and_channel_default(db_session):
    t = _tenant(db_session)
    cfg = TenantSalesAgentConfig.default_for(t.id)
    assert cfg.crm_webhook_url is None
    assert cfg.notify_channels.get("crm") is False
