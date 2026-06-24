"""Tests for GET/PATCH /api/v1/leads/{lead_id} and POST /api/v1/leads/{lead_id}/score."""

import uuid

from app.core.security import get_password_hash


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _make_tenant_user(db_session, label: str = ""):
    """Create a fresh Tenant + TENANT_ADMIN User; return (user, token)."""
    from app.core.security import create_access_token
    from app.db.models.tenant import Tenant
    from app.db.models.user import User, UserRole

    prefix = uuid.uuid4().hex[:8]
    tenant = Tenant(
        name=f"tenant_{prefix}",
        display_name=f"Tenant {prefix}",
        api_key_hash=uuid.uuid4().hex,
        subscription_tier="trial",
        subscription_status="trialing",
    )
    db_session.add(tenant)
    db_session.flush()

    user = User(
        email=f"user_{prefix}@example.com",
        hashed_password=get_password_hash("testpassword123"),
        full_name=f"User {prefix}",
        role=UserRole.TENANT_ADMIN,
        tenant_id=tenant.id,
        is_active=True,
    )
    db_session.add(user)
    db_session.flush()
    db_session.refresh(user)

    token = create_access_token(
        subject=str(user.id),
        additional_claims={
            "role": user.role.value,
            "tenant_id": str(user.tenant_id),
        },
    )
    return user, token


def _seed_leads(db_session, tenant_id, n: int, status: str = "new"):
    """Create n Lead rows for tenant_id, apply scores, and return the Lead objects."""
    from app.db.models.lead import Lead, LeadStatus
    from app.services.lead_service import LeadService

    svc = LeadService(db_session)
    leads = []
    for i in range(n):
        lead = Lead(
            tenant_id=tenant_id,
            name=f"Lead {i}",
            email=f"lead{i}_{uuid.uuid4().hex[:6]}@example.com",
            company=f"Company {i}" if i % 2 == 0 else None,
            intent="pricing" if i % 2 == 0 else None,
            status=LeadStatus(status),
        )
        db_session.add(lead)
        db_session.flush()
        svc.apply_score(lead)
        db_session.flush()
        db_session.refresh(lead)
        leads.append(lead)
    db_session.flush()
    return leads


# ---------------------------------------------------------------------------
# GET /api/v1/leads/{lead_id} — detail
# ---------------------------------------------------------------------------


def test_detail_cross_tenant_is_404(pg_client, db_session):
    """Cross-tenant lead lookup returns 404, not 403."""
    user_me, token_me = _make_tenant_user(db_session, "me")
    user_other, _token_other = _make_tenant_user(db_session, "other")

    other_leads = _seed_leads(db_session, user_other.tenant_id, n=1)
    other = other_leads[0]

    r = pg_client.get(f"/api/v1/leads/{other.id}", headers=_auth(token_me))
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "LEAD_NOT_FOUND"


def test_detail_breakdown_sums_to_score(pg_client, db_session):
    """score_breakdown fired weights sum to lead_score."""
    user, token = _make_tenant_user(db_session)
    leads = _seed_leads(db_session, user.tenant_id, n=1)
    lead = leads[0]

    resp = pg_client.get(f"/api/v1/leads/{lead.id}", headers=_auth(token))
    assert resp.status_code == 200
    b = resp.json()["data"]
    fired = sum(s["weight"] for s in b["score_breakdown"] if s["fired"])
    assert min(100, fired) == b["lead_score"]


def test_detail_has_required_fields(pg_client, db_session):
    """Detail response includes all LeadDetail fields."""
    user, token = _make_tenant_user(db_session)
    leads = _seed_leads(db_session, user.tenant_id, n=1)
    lead = leads[0]

    resp = pg_client.get(f"/api/v1/leads/{lead.id}", headers=_auth(token))
    assert resp.status_code == 200
    b = resp.json()["data"]
    assert {"id", "name", "company", "intent", "lead_score", "tier", "status",
             "source_label", "created_at", "email", "team_size", "industry",
             "conversation_id", "score_breakdown", "source", "meeting", "quote",
             "crm"} <= b.keys()


def test_detail_unauthenticated_returns_401(pg_client, db_session):
    """Unauthenticated requests get 401."""
    user, token = _make_tenant_user(db_session)
    leads = _seed_leads(db_session, user.tenant_id, n=1)
    lead = leads[0]

    resp = pg_client.get(f"/api/v1/leads/{lead.id}")
    assert resp.status_code == 401


def test_detail_lazy_backfill_applies_score(pg_client, db_session):
    """A lead with no score gets scored on first GET."""
    from app.db.models.lead import Lead, LeadStatus

    user, token = _make_tenant_user(db_session)
    lead = Lead(
        tenant_id=user.tenant_id,
        name="Unscored",
        email=f"unscored_{uuid.uuid4().hex[:6]}@example.com",
        status=LeadStatus.NEW,
        lead_score=None,
        score_breakdown=None,
    )
    db_session.add(lead)
    db_session.flush()
    db_session.refresh(lead)
    assert lead.lead_score is None

    resp = pg_client.get(f"/api/v1/leads/{lead.id}", headers=_auth(token))
    assert resp.status_code == 200
    b = resp.json()["data"]
    assert b["lead_score"] is not None
    assert isinstance(b["score_breakdown"], list)


# ---------------------------------------------------------------------------
# PATCH /api/v1/leads/{lead_id} — status update
# ---------------------------------------------------------------------------


def test_patch_status_valid_transition(pg_client, db_session):
    """PATCH with a valid transition updates status and returns the lead detail."""
    user, token = _make_tenant_user(db_session)
    leads = _seed_leads(db_session, user.tenant_id, n=1, status="new")
    lead = leads[0]

    ok = pg_client.patch(
        f"/api/v1/leads/{lead.id}",
        json={"status": "contacted"},
        headers=_auth(token),
    )
    assert ok.status_code == 200
    assert ok.json()["data"]["status"] == "contacted"


def test_patch_status_and_illegal_transition(pg_client, db_session):
    """Valid transition succeeds; going back to NEW from CONTACTED is 422."""
    user, token = _make_tenant_user(db_session)
    leads = _seed_leads(db_session, user.tenant_id, n=1, status="new")
    lead = leads[0]

    ok = pg_client.patch(
        f"/api/v1/leads/{lead.id}",
        json={"status": "contacted"},
        headers=_auth(token),
    )
    assert ok.status_code == 200
    assert ok.json()["data"]["status"] == "contacted"

    bad = pg_client.patch(
        f"/api/v1/leads/{lead.id}",
        json={"status": "new"},
        headers=_auth(token),
    )
    assert bad.status_code == 422


def test_patch_status_cross_tenant_is_404(pg_client, db_session):
    """Cannot patch another tenant's lead — 404."""
    user_me, token_me = _make_tenant_user(db_session, "me")
    user_other, _token_other = _make_tenant_user(db_session, "other")

    other_leads = _seed_leads(db_session, user_other.tenant_id, n=1)
    other = other_leads[0]

    r = pg_client.patch(
        f"/api/v1/leads/{other.id}",
        json={"status": "contacted"},
        headers=_auth(token_me),
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/v1/leads/{lead_id}/score — recompute
# ---------------------------------------------------------------------------


def test_recompute_returns_updated_score(pg_client, db_session):
    """POST /leads/{id}/score re-runs scoring and returns the lead detail."""
    user, token = _make_tenant_user(db_session)
    leads = _seed_leads(db_session, user.tenant_id, n=1)
    lead = leads[0]

    resp = pg_client.post(
        f"/api/v1/leads/{lead.id}/score",
        headers=_auth(token),
    )
    assert resp.status_code == 200
    b = resp.json()["data"]
    assert b["lead_score"] is not None
    fired = sum(s["weight"] for s in b["score_breakdown"] if s["fired"])
    assert min(100, fired) == b["lead_score"]


def test_recompute_cross_tenant_is_404(pg_client, db_session):
    """Cannot recompute another tenant's lead — 404."""
    user_me, token_me = _make_tenant_user(db_session, "me")
    user_other, _token_other = _make_tenant_user(db_session, "other")

    other_leads = _seed_leads(db_session, user_other.tenant_id, n=1)
    other = other_leads[0]

    r = pg_client.post(
        f"/api/v1/leads/{other.id}/score",
        headers=_auth(token_me),
    )
    assert r.status_code == 404


def test_recompute_unauthenticated_returns_401(pg_client, db_session):
    """Unauthenticated requests to /score get 401."""
    user, token = _make_tenant_user(db_session)
    leads = _seed_leads(db_session, user.tenant_id, n=1)
    lead = leads[0]

    resp = pg_client.post(f"/api/v1/leads/{lead.id}/score")
    assert resp.status_code == 401
