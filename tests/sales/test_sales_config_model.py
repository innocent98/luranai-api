"""TDD: TenantSalesAgentConfig model — round-trip + default_for classmethod."""

from uuid import uuid4

from app.db.models.sales_agent_config import TenantSalesAgentConfig
from app.db.models.tenant import Tenant


def _make_tenant(db_session) -> Tenant:
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
    return tenant


def test_config_round_trips(db_session):
    t = _make_tenant(db_session)
    cfg = TenantSalesAgentConfig.default_for(t.id)
    db_session.add(cfg)
    db_session.commit()
    db_session.refresh(cfg)

    assert cfg.currency == "NGN"
    assert "PRICING_INQUIRY" in cfg.trigger_intents
    assert cfg.notify_channels["email"] is True


def test_default_for_booleans(db_session):
    t = _make_tenant(db_session)
    cfg = TenantSalesAgentConfig.default_for(t.id)
    db_session.add(cfg)
    db_session.commit()
    db_session.refresh(cfg)

    assert cfg.agent_enabled is False
    assert cfg.lead_capture_enabled is True
    assert cfg.meeting_booking_enabled is False
    assert cfg.quotation_enabled is False


def test_default_for_notify_channels(db_session):
    t = _make_tenant(db_session)
    cfg = TenantSalesAgentConfig.default_for(t.id)
    db_session.add(cfg)
    db_session.commit()
    db_session.refresh(cfg)

    assert cfg.notify_channels == {"email": True, "slack": False, "in_app": False, "crm": False}


def test_default_for_trigger_intents(db_session):
    t = _make_tenant(db_session)
    cfg = TenantSalesAgentConfig.default_for(t.id)
    db_session.add(cfg)
    db_session.commit()
    db_session.refresh(cfg)

    assert len(cfg.trigger_intents) == 4
    assert "PRICING_INQUIRY" in cfg.trigger_intents
    assert "DEMO_REQUEST" in cfg.trigger_intents
    assert "PURCHASE_INTENT" in cfg.trigger_intents
    assert "ENTERPRISE_PROSPECT" in cfg.trigger_intents


def test_default_for_qualification_questions(db_session):
    t = _make_tenant(db_session)
    cfg = TenantSalesAgentConfig.default_for(t.id)
    db_session.add(cfg)
    db_session.commit()
    db_session.refresh(cfg)

    assert len(cfg.qualification_questions) == 3
    assert all(isinstance(q, dict) for q in cfg.qualification_questions)


def test_default_for_pricing_tiers_empty_list(db_session):
    t = _make_tenant(db_session)
    cfg = TenantSalesAgentConfig.default_for(t.id)
    db_session.add(cfg)
    db_session.commit()
    db_session.refresh(cfg)

    assert isinstance(cfg.pricing_tiers, list)


def test_unique_tenant_id_constraint(db_session):
    """Two configs for the same tenant must raise an IntegrityError."""
    import pytest
    from sqlalchemy.exc import IntegrityError

    t = _make_tenant(db_session)
    cfg1 = TenantSalesAgentConfig.default_for(t.id)
    cfg2 = TenantSalesAgentConfig.default_for(t.id)
    db_session.add(cfg1)
    db_session.flush()
    db_session.add(cfg2)

    with pytest.raises(IntegrityError):
        db_session.flush()
