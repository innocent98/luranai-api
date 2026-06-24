"""TDD: LeadService — list/detail/status/score orchestration."""

from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from app.db.models.lead import Lead, LeadStatus
from app.db.models.tenant import Tenant


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_tenant(db: Session) -> Tenant:
    unique = uuid4().hex[:8]
    tenant = Tenant(
        name=f"tenant_{unique}",
        display_name=f"Tenant {unique}",
        api_key_hash=uuid4().hex,
        subscription_tier="trial",
        subscription_status="trialing",
    )
    db.add(tenant)
    db.flush()
    db.refresh(tenant)
    return tenant


def _make_lead(db: Session, tenant: Tenant, **kwargs) -> Lead:
    lead = Lead(tenant_id=tenant.id, **kwargs)
    db.add(lead)
    db.flush()
    db.refresh(lead)
    return lead


# ---------------------------------------------------------------------------
# apply_score
# ---------------------------------------------------------------------------

def test_apply_score_sets_score_and_breakdown(db_session):
    from app.services.lead_service import LeadService

    tenant = _make_tenant(db_session)
    # team_size stored as String on model; apply_score must coerce it
    lead = Lead(tenant_id=tenant.id, email="a@b.com", company="Acme", team_size="120", industry="Bank")
    LeadService(db_session).apply_score(lead)

    # email(20) + company(10) + team_over_50(15) + enterprise/bank(20) = 65
    assert lead.lead_score == 65
    assert isinstance(lead.score_breakdown, list)
    assert any(b["key"] == "enterprise" and b["fired"] for b in lead.score_breakdown)


def test_apply_score_with_no_signals_defaults_correctly(db_session):
    from app.services.lead_service import LeadService

    tenant = _make_tenant(db_session)
    lead = Lead(tenant_id=tenant.id)  # no email, no company, etc.
    LeadService(db_session).apply_score(lead)

    assert lead.lead_score == 0
    assert all(not b["fired"] for b in lead.score_breakdown)


def test_apply_score_writes_breakdown_with_correct_shape(db_session):
    from app.services.lead_service import LeadService

    tenant = _make_tenant(db_session)
    lead = Lead(tenant_id=tenant.id, email="x@y.com")
    LeadService(db_session).apply_score(lead)

    assert lead.lead_score == 20
    for item in lead.score_breakdown:
        assert {"key", "label", "weight", "fired"} <= item.keys()


# ---------------------------------------------------------------------------
# list_leads — tenant scoping + filters
# ---------------------------------------------------------------------------

def test_list_leads_is_tenant_scoped(db_session):
    from app.services.lead_service import LeadService

    tenant_a = _make_tenant(db_session)
    tenant_b = _make_tenant(db_session)
    svc = LeadService(db_session)

    # 2 leads for tenant_a, 1 for tenant_b
    for tenant in (tenant_a, tenant_a, tenant_b):
        lead = Lead(tenant_id=tenant.id, email="x@y.com")
        svc.apply_score(lead)
        db_session.add(lead)
    db_session.commit()

    rows, total = svc.list_leads(tenant_a.id)
    assert total == 2
    assert all(r.tenant_id == tenant_a.id for r in rows)


def test_list_leads_filter_by_status(db_session):
    from app.services.lead_service import LeadService

    tenant = _make_tenant(db_session)
    svc = LeadService(db_session)

    lead_new = Lead(tenant_id=tenant.id, email="new@x.com", status=LeadStatus.NEW)
    lead_contacted = Lead(tenant_id=tenant.id, email="c@x.com", status=LeadStatus.CONTACTED)
    svc.apply_score(lead_new)
    svc.apply_score(lead_contacted)
    db_session.add(lead_new)
    db_session.add(lead_contacted)
    db_session.commit()

    rows, total = svc.list_leads(tenant.id, status=LeadStatus.CONTACTED)
    assert total == 1
    assert rows[0].status == LeadStatus.CONTACTED


def test_list_leads_filter_by_tier(db_session):
    """Tier filter: hot = score 61–85."""
    from app.services.lead_service import LeadService

    tenant = _make_tenant(db_session)
    svc = LeadService(db_session)

    # hot lead: email(20)+company(10)+team_over_50(15)+enterprise(20) = 65, tier=hot
    hot_lead = Lead(tenant_id=tenant.id, email="hot@x.com", company="Acme", team_size="120", industry="Bank")
    # cold lead: no signals
    cold_lead = Lead(tenant_id=tenant.id)
    svc.apply_score(hot_lead)
    svc.apply_score(cold_lead)
    db_session.add(hot_lead)
    db_session.add(cold_lead)
    db_session.commit()

    rows, total = svc.list_leads(tenant.id, tier="hot")
    assert total == 1
    assert rows[0].email == "hot@x.com"


def test_list_leads_default_sort_by_score_desc(db_session):
    from app.services.lead_service import LeadService

    tenant = _make_tenant(db_session)
    svc = LeadService(db_session)

    low = Lead(tenant_id=tenant.id, email="low@x.com")
    high = Lead(tenant_id=tenant.id, email="high@x.com", company="Acme")
    svc.apply_score(low)
    svc.apply_score(high)
    db_session.add(low)
    db_session.add(high)
    db_session.commit()

    rows, total = svc.list_leads(tenant.id, sort="score")
    assert total == 2
    assert rows[0].lead_score >= rows[1].lead_score


def test_list_leads_limit_and_offset(db_session):
    from app.services.lead_service import LeadService

    tenant = _make_tenant(db_session)
    svc = LeadService(db_session)

    for i in range(5):
        lead = Lead(tenant_id=tenant.id, email=f"lead{i}@x.com")
        svc.apply_score(lead)
        db_session.add(lead)
    db_session.commit()

    rows, total = svc.list_leads(tenant.id, limit=2, offset=0)
    assert total == 5
    assert len(rows) == 2


# ---------------------------------------------------------------------------
# get_lead
# ---------------------------------------------------------------------------

def test_get_lead_returns_correct_lead(db_session):
    from app.services.lead_service import LeadService

    tenant = _make_tenant(db_session)
    lead = _make_lead(db_session, tenant, email="find@x.com")
    svc = LeadService(db_session)

    result = svc.get_lead(tenant.id, lead.id)
    assert result is not None
    assert result.id == lead.id


def test_get_lead_cross_tenant_returns_none(db_session):
    from app.services.lead_service import LeadService

    tenant_a = _make_tenant(db_session)
    tenant_b = _make_tenant(db_session)
    lead = _make_lead(db_session, tenant_a, email="secure@x.com")
    svc = LeadService(db_session)

    result = svc.get_lead(tenant_b.id, lead.id)
    assert result is None


def test_get_lead_missing_returns_none(db_session):
    from app.services.lead_service import LeadService

    tenant = _make_tenant(db_session)
    svc = LeadService(db_session)

    result = svc.get_lead(tenant.id, uuid4())
    assert result is None


# ---------------------------------------------------------------------------
# set_status — transition validation
# ---------------------------------------------------------------------------

def test_set_status_valid_transition_new_to_contacted(db_session):
    from app.services.lead_service import LeadService

    tenant = _make_tenant(db_session)
    lead = _make_lead(db_session, tenant, email="t@x.com", status=LeadStatus.NEW)
    svc = LeadService(db_session)

    result = svc.set_status(tenant.id, lead.id, LeadStatus.CONTACTED)
    assert result.status == LeadStatus.CONTACTED


def test_set_status_valid_transition_contacted_to_qualified(db_session):
    from app.services.lead_service import LeadService

    tenant = _make_tenant(db_session)
    lead = _make_lead(db_session, tenant, email="t@x.com", status=LeadStatus.CONTACTED)
    svc = LeadService(db_session)

    result = svc.set_status(tenant.id, lead.id, LeadStatus.QUALIFIED)
    assert result.status == LeadStatus.QUALIFIED


def test_set_status_valid_transition_qualified_to_converted(db_session):
    from app.services.lead_service import LeadService

    tenant = _make_tenant(db_session)
    lead = _make_lead(db_session, tenant, email="t@x.com", status=LeadStatus.QUALIFIED)
    svc = LeadService(db_session)

    result = svc.set_status(tenant.id, lead.id, LeadStatus.CONVERTED)
    assert result.status == LeadStatus.CONVERTED


def test_illegal_status_transition_raises(db_session):
    from app.services.lead_service import LeadService

    tenant = _make_tenant(db_session)
    lead = _make_lead(db_session, tenant, email="t@x.com", status=LeadStatus.CONVERTED)
    svc = LeadService(db_session)

    with pytest.raises(ValueError):
        svc.set_status(tenant.id, lead.id, LeadStatus.NEW)


def test_illegal_status_transition_qualified_to_new_raises(db_session):
    from app.services.lead_service import LeadService

    tenant = _make_tenant(db_session)
    lead = _make_lead(db_session, tenant, email="t@x.com", status=LeadStatus.QUALIFIED)
    svc = LeadService(db_session)

    with pytest.raises(ValueError):
        svc.set_status(tenant.id, lead.id, LeadStatus.NEW)


def test_set_status_cross_tenant_raises_value_error(db_session):
    from app.services.lead_service import LeadService

    tenant_a = _make_tenant(db_session)
    tenant_b = _make_tenant(db_session)
    lead = _make_lead(db_session, tenant_a, email="t@x.com", status=LeadStatus.NEW)
    svc = LeadService(db_session)

    with pytest.raises(ValueError, match="not found"):
        svc.set_status(tenant_b.id, lead.id, LeadStatus.CONTACTED)


# ---------------------------------------------------------------------------
# recompute
# ---------------------------------------------------------------------------

def test_recompute_updates_score(db_session):
    from app.services.lead_service import LeadService

    tenant = _make_tenant(db_session)
    lead = _make_lead(db_session, tenant, email="r@x.com")
    svc = LeadService(db_session)

    svc.apply_score(lead)
    db_session.commit()
    db_session.refresh(lead)
    initial_score = lead.lead_score

    # Add company info to bump score
    lead.company = "Acme Corp"
    db_session.commit()

    updated = svc.recompute(tenant.id, lead.id)
    assert updated.lead_score > initial_score


def test_recompute_cross_tenant_raises_value_error(db_session):
    from app.services.lead_service import LeadService

    tenant_a = _make_tenant(db_session)
    tenant_b = _make_tenant(db_session)
    lead = _make_lead(db_session, tenant_a, email="r@x.com")
    svc = LeadService(db_session)

    with pytest.raises(ValueError, match="not found"):
        svc.recompute(tenant_b.id, lead.id)


# ---------------------------------------------------------------------------
# summary
# ---------------------------------------------------------------------------

def test_summary_counts_are_tenant_scoped(db_session):
    from app.services.lead_service import LeadService

    tenant_a = _make_tenant(db_session)
    tenant_b = _make_tenant(db_session)
    svc = LeadService(db_session)

    # 2 leads for tenant_a
    for email in ("s1@x.com", "s2@x.com"):
        lead = Lead(tenant_id=tenant_a.id, email=email)
        svc.apply_score(lead)
        db_session.add(lead)
    # 1 lead for tenant_b
    other = Lead(tenant_id=tenant_b.id, email="other@x.com")
    svc.apply_score(other)
    db_session.add(other)
    db_session.commit()

    result = svc.summary(tenant_a.id)
    assert result["status_counts"]["new"] == 2
    assert result["pipeline_value"] == 0
    assert "avg_score" in result
    assert "new_this_week" in result
    assert "sales_ready_count" in result


def test_summary_sales_ready_count(db_session):
    from app.services.lead_service import LeadService

    tenant = _make_tenant(db_session)
    svc = LeadService(db_session)

    # sales_ready requires score >= 86; max field signals give 70 so use conversation signals
    sales_lead = Lead(tenant_id=tenant.id, email="s@x.com", lead_score=90, score_breakdown=[])
    cold_lead = Lead(tenant_id=tenant.id, email="c@x.com", lead_score=10, score_breakdown=[])
    db_session.add(sales_lead)
    db_session.add(cold_lead)
    db_session.commit()

    result = svc.summary(tenant.id)
    assert result["sales_ready_count"] == 1
