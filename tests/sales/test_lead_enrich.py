"""TDD: LeadService.get_or_create_by_conversation + enrich_from_chat."""

import uuid

import pytest
from sqlalchemy.orm import Session

from app.db.models.lead import Lead, LeadStatus
from app.db.models.tenant import Tenant


# ---------------------------------------------------------------------------
# Helpers — mirror test_lead_service.py pattern (sync, no make_tenant fixture)
# ---------------------------------------------------------------------------

def _make_tenant(db: Session) -> Tenant:
    unique = uuid.uuid4().hex[:8]
    tenant = Tenant(
        name=f"tenant_{unique}",
        display_name=f"Tenant {unique}",
        api_key_hash=uuid.uuid4().hex,
        subscription_tier="trial",
        subscription_status="trialing",
    )
    db.add(tenant)
    db.flush()
    db.refresh(tenant)
    return tenant


# ---------------------------------------------------------------------------
# get_or_create_by_conversation
# ---------------------------------------------------------------------------

def test_get_or_create_is_idempotent_per_conversation(db_session):
    from app.services.lead_service import LeadService

    tenant = _make_tenant(db_session)
    conv = uuid.uuid4()
    svc = LeadService(db_session)

    a = svc.get_or_create_by_conversation(tenant.id, conv, intent="PRICING_INQUIRY")
    db_session.commit()
    b = svc.get_or_create_by_conversation(tenant.id, conv, intent="PRICING_INQUIRY")

    assert a.id == b.id


def test_get_or_create_creates_lead_with_normalized_intent(db_session):
    from app.services.lead_service import LeadService

    tenant = _make_tenant(db_session)
    conv = uuid.uuid4()
    svc = LeadService(db_session)

    lead = svc.get_or_create_by_conversation(tenant.id, conv, intent="pricing_inquiry")
    db_session.commit()

    assert lead.intent == "PRICING_INQUIRY"
    assert lead.status == LeadStatus.NEW
    assert lead.source == "chat_widget"
    assert lead.tenant_id == tenant.id
    assert lead.conversation_id == conv


def test_get_or_create_stores_utm_and_source(db_session):
    from app.services.lead_service import LeadService

    tenant = _make_tenant(db_session)
    conv = uuid.uuid4()
    svc = LeadService(db_session)

    utm = {"utm_source": "google", "utm_campaign": "brand"}
    lead = svc.get_or_create_by_conversation(
        tenant.id, conv, intent="DEMO_REQUEST", source="landing_page", utm=utm
    )
    db_session.commit()

    assert lead.source == "landing_page"
    assert lead.utm == utm


def test_get_or_create_scores_on_creation(db_session):
    from app.services.lead_service import LeadService

    tenant = _make_tenant(db_session)
    conv = uuid.uuid4()
    svc = LeadService(db_session)

    lead = svc.get_or_create_by_conversation(tenant.id, conv, intent="PRICING_INQUIRY")
    db_session.commit()

    # Score set (0 is valid; just assert it ran)
    assert lead.lead_score is not None
    assert isinstance(lead.score_breakdown, list)


def test_get_or_create_is_tenant_scoped(db_session):
    """Same conversation_id but different tenant_id -> two separate leads."""
    from app.services.lead_service import LeadService

    tenant_a = _make_tenant(db_session)
    tenant_b = _make_tenant(db_session)
    conv = uuid.uuid4()
    svc = LeadService(db_session)

    lead_a = svc.get_or_create_by_conversation(tenant_a.id, conv, intent="PRICING_INQUIRY")
    lead_b = svc.get_or_create_by_conversation(tenant_b.id, conv, intent="PRICING_INQUIRY")
    db_session.commit()

    assert lead_a.id != lead_b.id


# ---------------------------------------------------------------------------
# enrich_from_chat
# ---------------------------------------------------------------------------

def test_enrich_fills_and_rescores(db_session):
    from app.services.lead_service import LeadService

    tenant = _make_tenant(db_session)
    conv = uuid.uuid4()
    svc = LeadService(db_session)

    lead = svc.get_or_create_by_conversation(tenant.id, conv, intent="PRICING_INQUIRY")
    db_session.commit()
    before = lead.lead_score or 0

    svc.enrich_from_chat(
        lead,
        intent="ENTERPRISE_PROSPECT",
        captured={
            "name": "John",
            "email": "john@co.com",
            "company": "Co",
            "team_size": "200",
            "industry": "Bank",
        },
        signals={"requested_demo": True},
    )

    assert lead.email == "john@co.com"
    assert lead.name == "John"
    assert lead.company == "Co"
    assert lead.team_size == "200"
    assert lead.industry == "Bank"
    assert lead.intent == "ENTERPRISE_PROSPECT"
    # email(20)+company(10)+team_over_50(15)+enterprise(20)+demo(15) = 80 > 0
    assert (lead.lead_score or 0) > before


def test_enrich_ignores_blank_and_invalid_email(db_session):
    from app.services.lead_service import LeadService

    tenant = _make_tenant(db_session)
    conv = uuid.uuid4()
    svc = LeadService(db_session)

    lead = svc.get_or_create_by_conversation(tenant.id, conv, intent="PRICING_INQUIRY")
    lead.name = "Set"
    db_session.commit()

    svc.enrich_from_chat(
        lead,
        intent="PRICING_INQUIRY",
        captured={"name": None, "email": "not-an-email", "company": ""},
        signals={},
    )

    assert lead.name == "Set"          # not overwritten with null
    assert lead.email in (None, "")    # invalid email not stored


def test_enrich_does_not_overwrite_existing_email(db_session):
    """A later enrich with an empty email must not blank out an already-set email."""
    from app.services.lead_service import LeadService

    tenant = _make_tenant(db_session)
    conv = uuid.uuid4()
    svc = LeadService(db_session)

    lead = svc.get_or_create_by_conversation(tenant.id, conv, intent="PRICING_INQUIRY")
    db_session.commit()

    # First enrich sets a valid email
    svc.enrich_from_chat(
        lead,
        intent="PRICING_INQUIRY",
        captured={"email": "first@example.com"},
        signals={},
    )
    assert lead.email == "first@example.com"

    # Second enrich arrives with empty email — must not clobber
    svc.enrich_from_chat(
        lead,
        intent="PRICING_INQUIRY",
        captured={"email": ""},
        signals={},
    )
    assert lead.email == "first@example.com"


def test_enrich_intent_normalized(db_session):
    from app.services.lead_service import LeadService

    tenant = _make_tenant(db_session)
    conv = uuid.uuid4()
    svc = LeadService(db_session)

    lead = svc.get_or_create_by_conversation(tenant.id, conv, intent="PRICING_INQUIRY")
    db_session.commit()

    svc.enrich_from_chat(lead, intent="demo_request", captured={}, signals={})

    assert lead.intent == "DEMO_REQUEST"


def test_enrich_partial_captured_leaves_unset_fields_alone(db_session):
    """enrich_from_chat with only 'company' in captured must not touch name/email."""
    from app.services.lead_service import LeadService

    tenant = _make_tenant(db_session)
    conv = uuid.uuid4()
    svc = LeadService(db_session)

    lead = svc.get_or_create_by_conversation(tenant.id, conv, intent="PRICING_INQUIRY")
    lead.name = "Alice"
    lead.email = "alice@example.com"
    db_session.commit()

    svc.enrich_from_chat(
        lead,
        intent="PRICING_INQUIRY",
        captured={"company": "Acme"},
        signals={},
    )

    assert lead.name == "Alice"
    assert lead.email == "alice@example.com"
    assert lead.company == "Acme"
