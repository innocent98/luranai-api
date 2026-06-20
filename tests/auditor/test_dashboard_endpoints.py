"""Tests for authed tenant-dashboard auditor endpoints (Task 19)."""

import uuid
from unittest.mock import AsyncMock, patch


from app.db.models.audit import (
    AuditEntry,
    AuditEntryKind,
    AuditEntryStatus,
    AuditReport,
    AuditScope,
    AuditStatus,
)
from app.db.models.tenant import TenantCapabilities


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tenant_user(db_session):
    """Create a fresh tenant + TENANT_ADMIN user, return (user, token)."""
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


def _make_report(db_session, tenant_id, *, with_entries=False, gaps=None):
    """Create an AuditReport (COMPLETED) for tenant_id."""
    report = AuditReport(
        tenant_id=tenant_id,
        url="https://example.com",
        domain="example.com",
        scope=AuditScope.DASHBOARD,
        status=AuditStatus.COMPLETED,
        gaps=gaps or [],
    )
    db_session.add(report)
    db_session.flush()

    if with_entries:
        for i in range(3):
            db_session.add(
                AuditEntry(
                    audit_report_id=report.id,
                    tenant_id=tenant_id,
                    url=f"https://example.com/page{i}",
                    title=f"Page {i}",
                    category="faq",
                    kind=AuditEntryKind.FAQ,
                    content=f"content {i}",
                    content_hash=f"{i:064d}",
                    importance_rank=i + 1,
                    status=AuditEntryStatus.PROPOSED,
                )
            )
        db_session.flush()

    return report


# ---------------------------------------------------------------------------
# Auth guard
# ---------------------------------------------------------------------------


def test_post_audit_requires_auth(pg_client):
    resp = pg_client.post("/api/v1/audit", json={"url": "https://example.com"})
    assert resp.status_code in (401, 403)


def test_get_audit_requires_auth(pg_client):
    resp = pg_client.get("/api/v1/audit")
    assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# POST /audit — start dashboard audit
# ---------------------------------------------------------------------------


def test_post_audit_starts_dashboard_scope(pg_client, regular_user_token, db_session):
    with patch("app.auditor.service.enqueue_audit", new_callable=AsyncMock):
        resp = pg_client.post(
            "/api/v1/audit",
            json={"url": "https://example.com"},
            headers=_auth(regular_user_token),
        )
    assert resp.status_code == 202
    body = resp.json()
    assert body["success"] is True
    assert "audit_report_id" in body["data"]

    # Verify scope was set to DASHBOARD in DB
    report = db_session.query(AuditReport).filter(
        AuditReport.id == uuid.UUID(body["data"]["audit_report_id"])
    ).one()
    assert report.scope == AuditScope.DASHBOARD


def test_post_audit_enforces_monthly_limit(pg_client, db_session):
    """When tenant hits max_audits_per_month, POST /audit returns 403."""
    user, token = _make_tenant_user(db_session)

    # Set cap to 2
    db_session.add(TenantCapabilities(tenant_id=user.tenant_id, max_audits_per_month=2))
    db_session.flush()

    # Create 2 existing audits this month
    _make_report(db_session, user.tenant_id)
    _make_report(db_session, user.tenant_id)
    db_session.flush()

    # Third should be rejected
    with patch("app.auditor.service.enqueue_audit", new_callable=AsyncMock):
        resp = pg_client.post(
            "/api/v1/audit",
            json={"url": "https://example.com"},
            headers=_auth(token),
        )
    assert resp.status_code == 403
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "AUDIT_LIMIT_REACHED"


def test_post_audit_unlimited_when_no_cap(pg_client, db_session):
    """Tenant with no TenantCapabilities row is allowed through."""
    user, token = _make_tenant_user(db_session)
    # No TenantCapabilities row → unlimited

    with patch("app.auditor.service.enqueue_audit", new_callable=AsyncMock):
        resp = pg_client.post(
            "/api/v1/audit",
            json={"url": "https://example.com"},
            headers=_auth(token),
        )
    assert resp.status_code == 202


def test_post_audit_unlimited_when_cap_is_null(pg_client, db_session):
    """max_audits_per_month=None means unlimited."""
    user, token = _make_tenant_user(db_session)
    db_session.add(TenantCapabilities(tenant_id=user.tenant_id, max_audits_per_month=None))
    db_session.flush()

    with patch("app.auditor.service.enqueue_audit", new_callable=AsyncMock):
        resp = pg_client.post(
            "/api/v1/audit",
            json={"url": "https://example.com"},
            headers=_auth(token),
        )
    assert resp.status_code == 202


# ---------------------------------------------------------------------------
# GET /audit — history (tenant-scoped)
# ---------------------------------------------------------------------------


def test_list_audits_returns_only_own_tenant(pg_client, db_session):
    user_a, token_a = _make_tenant_user(db_session)
    user_b, token_b = _make_tenant_user(db_session)

    _make_report(db_session, user_a.tenant_id)
    _make_report(db_session, user_a.tenant_id)
    _make_report(db_session, user_b.tenant_id)
    db_session.flush()

    resp = pg_client.get("/api/v1/audit", headers=_auth(token_a))
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert len(body["data"]) == 2
    # All returned audits belong to tenant_a
    for item in body["data"]:
        assert "id" in item


# ---------------------------------------------------------------------------
# GET /audit/{id}/entries — tenant-scoped
# ---------------------------------------------------------------------------


def test_list_entries_returns_entries(pg_client, db_session):
    user, token = _make_tenant_user(db_session)
    report = _make_report(db_session, user.tenant_id, with_entries=True)
    db_session.flush()

    resp = pg_client.get(f"/api/v1/audit/{report.id}/entries", headers=_auth(token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert len(body["data"]) == 3


def test_list_entries_cross_tenant_blocked(pg_client, db_session):
    user_a, token_a = _make_tenant_user(db_session)
    user_b, token_b = _make_tenant_user(db_session)

    report_b = _make_report(db_session, user_b.tenant_id, with_entries=True)
    db_session.flush()

    # Tenant A tries to read Tenant B's entries
    resp = pg_client.get(f"/api/v1/audit/{report_b.id}/entries", headers=_auth(token_a))
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /audit/{id}/gaps — tenant-scoped
# ---------------------------------------------------------------------------


def test_list_gaps_returns_gaps(pg_client, db_session):
    user, token = _make_tenant_user(db_session)
    gaps = [{"title": "Gap 1", "kind": "faq", "demand_count": 5}]
    report = _make_report(db_session, user.tenant_id, gaps=gaps)
    db_session.flush()

    resp = pg_client.get(f"/api/v1/audit/{report.id}/gaps", headers=_auth(token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert len(body["data"]) == 1
    assert body["data"][0]["title"] == "Gap 1"


def test_list_gaps_cross_tenant_blocked(pg_client, db_session):
    user_a, token_a = _make_tenant_user(db_session)
    user_b, token_b = _make_tenant_user(db_session)

    report_b = _make_report(db_session, user_b.tenant_id, gaps=[{"title": "x"}])
    db_session.flush()

    resp = pg_client.get(f"/api/v1/audit/{report_b.id}/gaps", headers=_auth(token_a))
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# PATCH /audit/entries/{id} — edit entry
# ---------------------------------------------------------------------------


def test_patch_entry_edits_fields(pg_client, db_session):
    user, token = _make_tenant_user(db_session)
    report = _make_report(db_session, user.tenant_id, with_entries=True)
    db_session.flush()

    entry = db_session.query(AuditEntry).filter(
        AuditEntry.audit_report_id == report.id
    ).first()

    resp = pg_client.patch(
        f"/api/v1/audit/entries/{entry.id}",
        json={"content": "Updated content", "category": "policy"},
        headers=_auth(token),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["data"]["edited"] is True

    db_session.refresh(entry)
    assert entry.content == "Updated content"
    assert entry.category == "policy"
    assert entry.edited is True


def test_patch_entry_updates_title_and_kind(pg_client, db_session):
    """PATCH must update title and kind, and reject an invalid kind value."""
    user, token = _make_tenant_user(db_session)
    report = _make_report(db_session, user.tenant_id, with_entries=True)
    db_session.flush()

    entry = db_session.query(AuditEntry).filter(
        AuditEntry.audit_report_id == report.id
    ).first()

    # Valid update: change title and kind
    resp = pg_client.patch(
        f"/api/v1/audit/entries/{entry.id}",
        json={"title": "New Title", "kind": "policy"},
        headers=_auth(token),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["data"]["edited"] is True

    db_session.refresh(entry)
    assert entry.title == "New Title"
    assert entry.kind == AuditEntryKind.POLICY
    assert entry.edited is True

    # Invalid kind must be rejected with error envelope
    resp_bad = pg_client.patch(
        f"/api/v1/audit/entries/{entry.id}",
        json={"kind": "not_a_real_kind"},
        headers=_auth(token),
    )
    assert resp_bad.status_code == 422
    body_bad = resp_bad.json()
    assert body_bad["success"] is False
    assert body_bad["error"]["code"] == "INVALID_KIND"


def test_patch_entry_cross_tenant_blocked(pg_client, db_session):
    user_a, token_a = _make_tenant_user(db_session)
    user_b, token_b = _make_tenant_user(db_session)

    report_b = _make_report(db_session, user_b.tenant_id, with_entries=True)
    db_session.flush()

    entry_b = db_session.query(AuditEntry).filter(
        AuditEntry.audit_report_id == report_b.id
    ).first()

    # Tenant A tries to edit Tenant B's entry
    resp = pg_client.patch(
        f"/api/v1/audit/entries/{entry_b.id}",
        json={"content": "Hacked"},
        headers=_auth(token_a),
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /audit/{id}/import — import entries
# ---------------------------------------------------------------------------


def test_import_audit_calls_import_entries(pg_client, db_session):
    user, token = _make_tenant_user(db_session)
    db_session.add(TenantCapabilities(tenant_id=user.tenant_id, max_knowledge_sources=10))
    db_session.flush()

    report = _make_report(db_session, user.tenant_id, with_entries=True)
    db_session.flush()

    with patch("app.auditor.service.enqueue_document_ingestion", new_callable=AsyncMock):
        resp = pg_client.post(f"/api/v1/audit/{report.id}/import", headers=_auth(token))

    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert "imported_count" in body["data"]


def test_import_audit_cross_tenant_blocked(pg_client, db_session):
    user_a, token_a = _make_tenant_user(db_session)
    user_b, token_b = _make_tenant_user(db_session)

    report_b = _make_report(db_session, user_b.tenant_id, with_entries=True)
    db_session.flush()

    resp = pg_client.post(f"/api/v1/audit/{report_b.id}/import", headers=_auth(token_a))
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /audit/{id}/gaps/{idx}/outline — forward stub (501)
# ---------------------------------------------------------------------------


def test_generate_outline_is_forward_stub(pg_client, db_session):
    user, token = _make_tenant_user(db_session)
    report = _make_report(
        db_session, user.tenant_id, gaps=[{"title": "x", "kind": "faq", "demand_count": 1}]
    )
    db_session.flush()

    resp = pg_client.post(
        f"/api/v1/audit/{report.id}/gaps/0/outline", headers=_auth(token)
    )
    assert resp.status_code == 501
    assert "Stage 6" in resp.json()["detail"]


def test_generate_outline_cross_tenant_returns_404(pg_client, db_session):
    """Ownership check fires before the 501 stub — cross-tenant gets 404."""
    user_a, token_a = _make_tenant_user(db_session)
    user_b, token_b = _make_tenant_user(db_session)

    report_a = _make_report(
        db_session, user_a.tenant_id, gaps=[{"title": "x", "kind": "faq", "demand_count": 1}]
    )
    db_session.flush()

    resp = pg_client.post(
        f"/api/v1/audit/{report_a.id}/gaps/0/outline", headers=_auth(token_b)
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Cross-tenant isolation: full audit lifecycle
# ---------------------------------------------------------------------------


def test_patch_active_limit_rejected_when_at_cap(pg_client, db_session):
    """PATCH a 3rd entry to ACTIVE with max_knowledge_sources=2 → 403 ACTIVE_LIMIT_REACHED."""
    user, token = _make_tenant_user(db_session)
    db_session.add(TenantCapabilities(tenant_id=user.tenant_id, max_knowledge_sources=2))
    db_session.flush()

    report = _make_report(db_session, user.tenant_id, with_entries=True)
    db_session.flush()

    entries = (
        db_session.query(AuditEntry)
        .filter(AuditEntry.audit_report_id == report.id)
        .order_by(AuditEntry.importance_rank.asc())
        .all()
    )
    # Manually activate first two entries (simulating preselect)
    entries[0].status = AuditEntryStatus.ACTIVE
    entries[1].status = AuditEntryStatus.ACTIVE
    entries[2].status = AuditEntryStatus.LOCKED
    db_session.flush()

    # Attempt to activate the 3rd entry → should be rejected
    resp = pg_client.patch(
        f"/api/v1/audit/entries/{entries[2].id}",
        json={"status": "active"},
        headers=_auth(token),
    )
    assert resp.status_code == 403
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "ACTIVE_LIMIT_REACHED"


def test_patch_active_swap_succeeds_at_cap(pg_client, db_session):
    """Lock one entry then activate another (a swap) still succeeds at limit=2."""
    user, token = _make_tenant_user(db_session)
    db_session.add(TenantCapabilities(tenant_id=user.tenant_id, max_knowledge_sources=2))
    db_session.flush()

    report = _make_report(db_session, user.tenant_id, with_entries=True)
    db_session.flush()

    entries = (
        db_session.query(AuditEntry)
        .filter(AuditEntry.audit_report_id == report.id)
        .order_by(AuditEntry.importance_rank.asc())
        .all()
    )
    entries[0].status = AuditEntryStatus.ACTIVE
    entries[1].status = AuditEntryStatus.ACTIVE
    entries[2].status = AuditEntryStatus.LOCKED
    db_session.flush()

    # Step 1: lock one of the active entries (now 1 active)
    resp = pg_client.patch(
        f"/api/v1/audit/entries/{entries[0].id}",
        json={"status": "locked"},
        headers=_auth(token),
    )
    assert resp.status_code == 200

    # Step 2: activate the previously locked entry (back to 2 active) → allowed
    resp = pg_client.patch(
        f"/api/v1/audit/entries/{entries[2].id}",
        json={"status": "active"},
        headers=_auth(token),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True


def test_cross_tenant_audit_full_isolation(pg_client, db_session):
    """Tenant B cannot read Tenant A's audit, entries, gaps, or import them."""
    user_a, token_a = _make_tenant_user(db_session)
    user_b, token_b = _make_tenant_user(db_session)

    report_a = _make_report(
        db_session, user_a.tenant_id, with_entries=True, gaps=[{"title": "g"}]
    )
    db_session.flush()

    audit_id = report_a.id

    # Tenant B cannot see Tenant A's entries
    r = pg_client.get(f"/api/v1/audit/{audit_id}/entries", headers=_auth(token_b))
    assert r.status_code == 404

    # Tenant B cannot see Tenant A's gaps
    r = pg_client.get(f"/api/v1/audit/{audit_id}/gaps", headers=_auth(token_b))
    assert r.status_code == 404

    # Tenant B cannot import Tenant A's audit
    r = pg_client.post(f"/api/v1/audit/{audit_id}/import", headers=_auth(token_b))
    assert r.status_code == 404

    # Tenant B's history does not include Tenant A's audit
    r = pg_client.get("/api/v1/audit", headers=_auth(token_b))
    assert r.status_code == 200
    ids = [item["id"] for item in r.json()["data"]]
    assert str(audit_id) not in ids
