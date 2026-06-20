"""Tests for GET /api/v1/audit/entries/{entry_id} (entry-detail endpoint)."""

import uuid

from app.core.security import get_password_hash
from app.db.models.audit import (
    AuditEntry,
    AuditEntryKind,
    AuditEntryStatus,
    AuditReport,
    AuditScope,
    AuditStatus,
)


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


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


def _make_entry(db_session, tenant_id, content="Full body text"):
    """Create an AuditReport + one AuditEntry for tenant_id and return the entry."""
    report = AuditReport(
        tenant_id=tenant_id,
        url="https://example.com",
        domain="example.com",
        scope=AuditScope.DASHBOARD,
        status=AuditStatus.COMPLETED,
        gaps=[],
    )
    db_session.add(report)
    db_session.flush()

    entry = AuditEntry(
        audit_report_id=report.id,
        tenant_id=tenant_id,
        url="https://example.com/page",
        title="Test Page",
        category="faq",
        kind=AuditEntryKind.FAQ,
        content=content,
        content_hash="a" * 64,
        importance_rank=1,
        status=AuditEntryStatus.PROPOSED,
    )
    db_session.add(entry)
    db_session.flush()
    db_session.refresh(entry)
    return entry


# ---------------------------------------------------------------------------
# GET /audit/entries/{entry_id}
# ---------------------------------------------------------------------------


def test_get_entry_detail_returns_full_content(pg_client, db_session):
    user, token = _make_tenant_user(db_session)
    entry = _make_entry(db_session, user.tenant_id, content="Full body text")

    resp = pg_client.get(
        f"/api/v1/audit/entries/{entry.id}",
        headers=_auth(token),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    data = body["data"]
    assert data["id"] == str(entry.id)
    assert data["content"] == "Full body text"
    assert set(data) >= {
        "id", "url", "title", "category", "kind",
        "content", "status", "importance_rank", "edited",
    }


def test_get_entry_detail_cross_tenant_returns_404(pg_client, db_session):
    _user_a, token_a = _make_tenant_user(db_session)
    user_b, _token_b = _make_tenant_user(db_session)

    # Entry belongs to tenant B; requester is tenant A
    entry_b = _make_entry(db_session, user_b.tenant_id)

    resp = pg_client.get(
        f"/api/v1/audit/entries/{entry_b.id}",
        headers=_auth(token_a),
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "ENTRY_NOT_FOUND"


def test_get_entry_detail_unauthenticated_returns_401(pg_client, db_session):
    _user, _token = _make_tenant_user(db_session)
    entry = _make_entry(db_session, _user.tenant_id)

    resp = pg_client.get(f"/api/v1/audit/entries/{entry.id}")
    assert resp.status_code == 401
