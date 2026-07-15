"""Tests for GET /api/v1/leads and GET /api/v1/leads/summary."""

import uuid

def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _make_tenant_user(db_session):
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
        hashed_password="$2b$12$placeholder_hash_not_real",
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


def _seed_leads(db_session, tenant_id, n: int):
    """Create n Lead rows for tenant_id with ascending scores and flush."""
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
            status=LeadStatus.NEW,
        )
        db_session.add(lead)
        db_session.flush()
        svc.apply_score(lead)
    db_session.flush()
    return leads


# ---------------------------------------------------------------------------
# GET /api/v1/leads
# ---------------------------------------------------------------------------


def test_list_returns_only_callers_tenant(pg_client, db_session):
    """Only the calling tenant's leads are returned, not another tenant's."""
    user_me, token_me = _make_tenant_user(db_session)
    user_other, _token_other = _make_tenant_user(db_session)

    _seed_leads(db_session, user_me.tenant_id, n=3)
    _seed_leads(db_session, user_other.tenant_id, n=2)
    db_session.flush()

    resp = pg_client.get("/api/v1/leads", headers=_auth(token_me))
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    data = body["data"]
    assert data["total"] == 3
    assert len(data["items"]) == 3


def test_list_default_sort_score_desc(pg_client, db_session):
    """Default sort is score descending (highest score first)."""
    user, token = _make_tenant_user(db_session)
    _seed_leads(db_session, user.tenant_id, n=3)
    db_session.flush()

    resp = pg_client.get("/api/v1/leads", headers=_auth(token))
    assert resp.status_code == 200
    items = resp.json()["data"]["items"]
    scores = [item["lead_score"] for item in items if item["lead_score"] is not None]
    assert scores == sorted(scores, reverse=True)


def test_list_response_envelope_shape(pg_client, db_session):
    """Response has the standard success envelope with data.items/total/limit/offset."""
    user, token = _make_tenant_user(db_session)
    _seed_leads(db_session, user.tenant_id, n=2)
    db_session.flush()

    resp = pg_client.get("/api/v1/leads", headers=_auth(token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    data = body["data"]
    assert {"items", "total", "limit", "offset"} <= data.keys()


def test_list_item_fields(pg_client, db_session):
    """Each list item exposes the required fields including tier and source_label."""
    user, token = _make_tenant_user(db_session)
    _seed_leads(db_session, user.tenant_id, n=1)
    db_session.flush()

    resp = pg_client.get("/api/v1/leads", headers=_auth(token))
    assert resp.status_code == 200
    item = resp.json()["data"]["items"][0]
    assert {"id", "name", "company", "intent", "lead_score", "tier", "status", "source_label", "created_at"} <= item.keys()


def test_list_unauthenticated_returns_401(pg_client):
    """Unauthenticated requests get 401."""
    resp = pg_client.get("/api/v1/leads")
    assert resp.status_code == 401


def test_list_empty_tenant(pg_client, db_session):
    """A tenant with no leads gets total=0 and empty items list."""
    user, token = _make_tenant_user(db_session)

    resp = pg_client.get("/api/v1/leads", headers=_auth(token))
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["total"] == 0
    assert data["items"] == []


def test_list_filter_by_status(pg_client, db_session):
    """status= query param filters correctly."""
    from app.db.models.lead import Lead, LeadStatus

    user, token = _make_tenant_user(db_session)

    lead_new = Lead(
        tenant_id=user.tenant_id,
        name="New Lead",
        email=f"new_{uuid.uuid4().hex[:6]}@example.com",
        status=LeadStatus.NEW,
    )
    lead_contacted = Lead(
        tenant_id=user.tenant_id,
        name="Contacted Lead",
        email=f"contacted_{uuid.uuid4().hex[:6]}@example.com",
        status=LeadStatus.CONTACTED,
    )
    db_session.add_all([lead_new, lead_contacted])
    db_session.flush()

    resp = pg_client.get("/api/v1/leads?status=new", headers=_auth(token))
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["total"] == 1
    assert data["items"][0]["status"] == "new"


def test_list_pagination(pg_client, db_session):
    """limit and offset params are respected."""
    user, token = _make_tenant_user(db_session)
    _seed_leads(db_session, user.tenant_id, n=5)
    db_session.flush()

    resp = pg_client.get("/api/v1/leads?limit=2&offset=0", headers=_auth(token))
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["total"] == 5
    assert len(data["items"]) == 2
    assert data["limit"] == 2
    assert data["offset"] == 0


# ---------------------------------------------------------------------------
# GET /api/v1/leads/summary
# ---------------------------------------------------------------------------


def test_summary_shape(pg_client, db_session):
    """Summary returns the required keys."""
    user, token = _make_tenant_user(db_session)
    _seed_leads(db_session, user.tenant_id, n=5)
    db_session.flush()

    resp = pg_client.get("/api/v1/leads/summary", headers=_auth(token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    data = body["data"]
    assert {"new_this_week", "avg_score", "sales_ready_count", "pipeline_value", "status_counts"} <= data.keys()
    assert data["pipeline_value"] == 0


def test_summary_unauthenticated_returns_401(pg_client):
    """Unauthenticated requests get 401."""
    resp = pg_client.get("/api/v1/leads/summary")
    assert resp.status_code == 401


def test_summary_tenant_isolation(pg_client, db_session):
    """Summary only counts the calling tenant's leads."""
    user_me, token_me = _make_tenant_user(db_session)
    user_other, _ = _make_tenant_user(db_session)

    _seed_leads(db_session, user_me.tenant_id, n=3)
    _seed_leads(db_session, user_other.tenant_id, n=10)
    db_session.flush()

    resp = pg_client.get("/api/v1/leads/summary", headers=_auth(token_me))
    assert resp.status_code == 200
    data = resp.json()["data"]
    # new_this_week should reflect only caller's 3 leads, not the other's 10
    assert data["new_this_week"] == 3
