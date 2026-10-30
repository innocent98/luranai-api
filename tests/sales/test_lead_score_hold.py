"""A later chat turn must never lower a lead's score/tier; humans still can."""

import uuid

import pytest

from app.db.models.lead import LeadStatus
from app.db.models.notification_log import NotificationLog, NotifEvent
from app.db.models.sales_agent_config import TenantSalesAgentConfig
from app.db.models.tenant import Tenant
from app.sales.scoring import tier_for
from app.services.lead_service import LeadService
from app.services.notification_service import NotificationService

STRONG = {"asked_features": True, "mentioned_competitor": True, "ready_to_buy": True}
CAPTURED = {"name": "Ada", "email": "ada@example.com", "company": "Acme Group Logistics"}
NO_CAPTURE = {k: None for k in ("name", "email", "company", "team_size", "industry")}


def _tenant(db) -> Tenant:
    u = uuid.uuid4().hex[:8]
    t = Tenant(name=f"t_{u}", display_name=f"T {u}", api_key_hash=u,
               subscription_tier="trial", subscription_status="trialing")
    db.add(t)
    db.flush()
    return t


def _lead(db, svc, tenant):
    lead = svc.get_or_create_by_conversation(tenant.id, uuid.uuid4(), intent="PRICING_INQUIRY")
    db.commit()
    return lead


def test_new_signals_raise_the_score(db_session):
    svc = LeadService(db_session)
    lead = _lead(db_session, svc, _tenant(db_session))
    svc.enrich_from_chat(lead, intent="PRICING_INQUIRY", captured=NO_CAPTURE, signals={})
    low = lead.lead_score
    svc.enrich_from_chat(lead, intent="PURCHASE_INTENT", captured=CAPTURED, signals=STRONG)
    assert lead.lead_score > low
    assert lead.lead_score >= 86 and tier_for(lead.lead_score) == "sales_ready"


def test_a_plain_later_turn_holds_score_breakdown_and_tier(db_session):
    svc = LeadService(db_session)
    lead = _lead(db_session, svc, _tenant(db_session))
    svc.enrich_from_chat(lead, intent="PURCHASE_INTENT", captured=CAPTURED, signals=STRONG)
    peak, breakdown = lead.lead_score, list(lead.score_breakdown)
    assert peak >= 86

    # A demo-request turn with fewer signals used to recompute this to 70 ("hot").
    svc.enrich_from_chat(lead, intent="DEMO_REQUEST", captured=NO_CAPTURE,
                         signals={"requested_demo": True})
    assert lead.lead_score == peak
    assert lead.score_breakdown == breakdown
    assert tier_for(lead.lead_score) == "sales_ready"
    # The latest intent is still recorded: only the score is held.
    assert lead.intent == "DEMO_REQUEST"


def test_a_higher_recompute_replaces_the_held_score(db_session):
    svc = LeadService(db_session)
    lead = _lead(db_session, svc, _tenant(db_session))
    svc.enrich_from_chat(lead, intent="PRICING_INQUIRY", captured=CAPTURED, signals={})
    mid = lead.lead_score
    svc.enrich_from_chat(lead, intent="PURCHASE_INTENT", captured=NO_CAPTURE,
                         signals={"ready_to_buy": True})
    assert lead.lead_score > mid
    assert {b["key"]: b["fired"] for b in lead.score_breakdown}["ready_to_buy"] is True


def test_explicit_rescore_by_a_rep_is_still_honoured_as_a_downgrade(db_session):
    svc = LeadService(db_session)
    tenant = _tenant(db_session)
    lead = _lead(db_session, svc, tenant)
    svc.enrich_from_chat(lead, intent="PURCHASE_INTENT", captured=CAPTURED, signals=STRONG)
    assert lead.lead_score >= 86

    lowered = svc.recompute(tenant.id, lead.id)  # POST /leads/{id}/score
    assert lowered.lead_score < 86

    # ...and the next chat turn holds the NEW figure rather than resurrecting the old peak.
    svc.enrich_from_chat(lead, intent="SUPPORT", captured=NO_CAPTURE, signals={})
    assert lead.lead_score == lowered.lead_score


def test_rep_status_changes_are_never_overridden_by_chat_turns(db_session):
    svc = LeadService(db_session)
    tenant = _tenant(db_session)
    lead = _lead(db_session, svc, tenant)
    svc.set_status(tenant.id, lead.id, LeadStatus.CONVERTED)
    svc.enrich_from_chat(lead, intent="PURCHASE_INTENT", captured=CAPTURED, signals=STRONG)
    svc.enrich_from_chat(lead, intent="SUPPORT", captured=NO_CAPTURE, signals={})
    assert lead.status == LeadStatus.CONVERTED


def test_no_second_alert_when_the_held_score_is_seen_again(db_session):
    svc = LeadService(db_session)
    tenant = _tenant(db_session)
    cfg = TenantSalesAgentConfig.default_for(tenant.id)
    cfg.notify_channels = {"email": False, "in_app": True, "slack": False, "crm": False}
    db_session.add(cfg)
    lead = _lead(db_session, svc, tenant)

    prior = lead.lead_score
    svc.enrich_from_chat(lead, intent="PURCHASE_INTENT", captured=CAPTURED, signals=STRONG)
    assert svc.crossed_sales_ready(prior, lead) is True
    NotificationService(db_session).dispatch(
        tenant.id, NotifEvent.SALES_READY_LEAD, entity_type="lead", entity_id=lead.id
    )

    prior = lead.lead_score
    svc.enrich_from_chat(lead, intent="DEMO_REQUEST", captured=NO_CAPTURE, signals={})
    assert svc.crossed_sales_ready(prior, lead) is False  # held at the peak: no re-crossing

    # Even a dispatch forced anyway is deduped by the unique index.
    NotificationService(db_session).dispatch(
        tenant.id, NotifEvent.SALES_READY_LEAD, entity_type="lead", entity_id=lead.id
    )
    rows = db_session.query(NotificationLog).filter_by(
        tenant_id=tenant.id, event=NotifEvent.SALES_READY_LEAD
    ).count()
    assert rows == 1


def test_a_lead_pushed_below_the_threshold_by_a_rep_can_cross_again(db_session):
    svc = LeadService(db_session)
    tenant = _tenant(db_session)
    lead = _lead(db_session, svc, tenant)
    svc.enrich_from_chat(lead, intent="PURCHASE_INTENT", captured=CAPTURED, signals=STRONG)
    svc.recompute(tenant.id, lead.id)  # rep rescore drops it
    prior = lead.lead_score
    assert prior < 86
    svc.enrich_from_chat(lead, intent="PURCHASE_INTENT", captured=NO_CAPTURE, signals=STRONG)
    assert svc.crossed_sales_ready(prior, lead) is True
