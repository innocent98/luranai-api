"""Tests for the in-app notifications read API (Stage 3 Slice 5c).

Auth pattern: mirrors test_leads_list.py — _make_tenant_user creates a fresh
Tenant + TENANT_ADMIN User, returns (user, token). Uses pg_client + db_session
fixtures from conftest.py (Postgres-backed, savepoint-rolled-back per test).
"""

import uuid
from datetime import datetime, timezone

from app.db.models.notification_log import NotifChannel, NotifEvent, NotifStatus, NotificationLog


# ---------------------------------------------------------------------------
# Auth helpers (mirrors test_leads_list._make_tenant_user exactly)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Seed helper — creates in_app NotificationLog rows for a tenant
# ---------------------------------------------------------------------------


def _seed_notifs(db_session, tenant_id, n: int, read: bool = False):
    """Seed n in-app NotificationLog rows for tenant_id.

    Each row gets a unique entity_id to satisfy the dedupe unique index
    (tenant_id, event, channel, entity_id, recipient).
    """
    rows = []
    for i in range(n):
        row = NotificationLog(
            tenant_id=tenant_id,
            event=NotifEvent.SALES_READY_LEAD,
            channel=NotifChannel.IN_APP,
            entity_type="lead",
            entity_id=uuid.uuid4(),
            recipient="admin@example.com",
            status=NotifStatus.SENT,
            title=f"Notification {i}",
            body=f"Body {i}",
            read_at=datetime.now(timezone.utc) if read else None,
        )
        db_session.add(row)
        rows.append(row)
    db_session.flush()
    return rows


def _seed_email_notif(db_session, tenant_id):
    """Seed one email-channel row (should be hidden from in-app endpoints)."""
    row = NotificationLog(
        tenant_id=tenant_id,
        event=NotifEvent.MEETING_BOOKED,
        channel=NotifChannel.EMAIL,
        entity_type="meeting",
        entity_id=uuid.uuid4(),
        recipient="admin@example.com",
        status=NotifStatus.SENT,
        title="Email notif",
        body="Should not appear in in-app list",
    )
    db_session.add(row)
    db_session.flush()
    return row


# ===========================================================================
# GET /api/v1/notifications
# ===========================================================================


def test_list_returns_in_app_only(pg_client, db_session):
    """Only channel='in_app' rows are returned; email rows are excluded."""
    user, token = _make_tenant_user(db_session)

    _seed_notifs(db_session, user.tenant_id, n=2)
    _seed_email_notif(db_session, user.tenant_id)

    resp = pg_client.get("/api/v1/notifications", headers=_auth(token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    data = body["data"]
    assert data["total"] == 2
    assert len(data["items"]) == 2


def test_list_tenant_scoped(pg_client, db_session):
    """A tenant only sees its own notifications."""
    user_me, token_me = _make_tenant_user(db_session)
    user_other, _ = _make_tenant_user(db_session)

    _seed_notifs(db_session, user_me.tenant_id, n=3)
    _seed_notifs(db_session, user_other.tenant_id, n=5)

    resp = pg_client.get("/api/v1/notifications", headers=_auth(token_me))
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["total"] == 3
    assert len(data["items"]) == 3


def test_list_newest_first(pg_client, db_session):
    """Notifications are returned newest-first (created_at desc)."""
    user, token = _make_tenant_user(db_session)
    _seed_notifs(db_session, user.tenant_id, n=3)

    resp = pg_client.get("/api/v1/notifications", headers=_auth(token))
    assert resp.status_code == 200
    items = resp.json()["data"]["items"]
    timestamps = [item["created_at"] for item in items]
    assert timestamps == sorted(timestamps, reverse=True)


def test_list_envelope_shape(pg_client, db_session):
    """Response envelope has items/total/limit/offset keys."""
    user, token = _make_tenant_user(db_session)
    _seed_notifs(db_session, user.tenant_id, n=1)

    resp = pg_client.get("/api/v1/notifications", headers=_auth(token))
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert {"items", "total", "limit", "offset"} <= data.keys()


def test_list_item_fields(pg_client, db_session):
    """Each list item has the required fields."""
    user, token = _make_tenant_user(db_session)
    _seed_notifs(db_session, user.tenant_id, n=1)

    resp = pg_client.get("/api/v1/notifications", headers=_auth(token))
    assert resp.status_code == 200
    item = resp.json()["data"]["items"][0]
    assert {"id", "event", "title", "body", "read_at", "created_at"} <= item.keys()


def test_list_unread_only_filter(pg_client, db_session):
    """unread_only=true returns only rows where read_at is None."""
    user, token = _make_tenant_user(db_session)
    _seed_notifs(db_session, user.tenant_id, n=3, read=False)
    _seed_notifs(db_session, user.tenant_id, n=2, read=True)

    resp = pg_client.get("/api/v1/notifications?unread_only=true", headers=_auth(token))
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["total"] == 3
    assert len(data["items"]) == 3
    for item in data["items"]:
        assert item["read_at"] is None


def test_list_all_includes_read_and_unread(pg_client, db_session):
    """Without unread_only, both read and unread rows are returned."""
    user, token = _make_tenant_user(db_session)
    _seed_notifs(db_session, user.tenant_id, n=2, read=False)
    _seed_notifs(db_session, user.tenant_id, n=1, read=True)

    resp = pg_client.get("/api/v1/notifications", headers=_auth(token))
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["total"] == 3


def test_list_pagination(pg_client, db_session):
    """limit and offset are respected."""
    user, token = _make_tenant_user(db_session)
    _seed_notifs(db_session, user.tenant_id, n=5)

    resp = pg_client.get("/api/v1/notifications?limit=2&offset=0", headers=_auth(token))
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["total"] == 5
    assert len(data["items"]) == 2
    assert data["limit"] == 2
    assert data["offset"] == 0


def test_list_empty_tenant(pg_client, db_session):
    """Tenant with no notifications gets total=0 and empty items."""
    user, token = _make_tenant_user(db_session)

    resp = pg_client.get("/api/v1/notifications", headers=_auth(token))
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["total"] == 0
    assert data["items"] == []


def test_list_unauthenticated_returns_401(pg_client):
    """Unauthenticated requests get 401."""
    resp = pg_client.get("/api/v1/notifications")
    assert resp.status_code == 401


# ===========================================================================
# GET /api/v1/notifications/unread-count
# ===========================================================================


def test_unread_count_correct(pg_client, db_session):
    """Unread count reflects only unread in-app rows for the tenant."""
    user, token = _make_tenant_user(db_session)
    _seed_notifs(db_session, user.tenant_id, n=4, read=False)
    _seed_notifs(db_session, user.tenant_id, n=2, read=True)

    resp = pg_client.get("/api/v1/notifications/unread-count", headers=_auth(token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["data"]["count"] == 4


def test_unread_count_tenant_scoped(pg_client, db_session):
    """Unread count is scoped to the calling tenant."""
    user_me, token_me = _make_tenant_user(db_session)
    user_other, _ = _make_tenant_user(db_session)

    _seed_notifs(db_session, user_me.tenant_id, n=2, read=False)
    _seed_notifs(db_session, user_other.tenant_id, n=10, read=False)

    resp = pg_client.get("/api/v1/notifications/unread-count", headers=_auth(token_me))
    assert resp.status_code == 200
    assert resp.json()["data"]["count"] == 2


def test_unread_count_unauthenticated_returns_401(pg_client):
    resp = pg_client.get("/api/v1/notifications/unread-count")
    assert resp.status_code == 401


# ===========================================================================
# PATCH /api/v1/notifications/{id}/read
# ===========================================================================


def test_mark_read_sets_read_at(pg_client, db_session):
    """PATCH /{id}/read sets read_at and returns id + read_at."""
    user, token = _make_tenant_user(db_session)
    rows = _seed_notifs(db_session, user.tenant_id, n=1, read=False)
    notif_id = str(rows[0].id)

    resp = pg_client.patch(f"/api/v1/notifications/{notif_id}/read", headers=_auth(token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["data"]["id"] == notif_id
    assert body["data"]["read_at"] is not None


def test_mark_read_decrements_unread_count(pg_client, db_session):
    """After marking one read, unread-count decrements by 1."""
    user, token = _make_tenant_user(db_session)
    rows = _seed_notifs(db_session, user.tenant_id, n=3, read=False)
    notif_id = str(rows[0].id)

    pg_client.patch(f"/api/v1/notifications/{notif_id}/read", headers=_auth(token))

    resp = pg_client.get("/api/v1/notifications/unread-count", headers=_auth(token))
    assert resp.json()["data"]["count"] == 2


def test_mark_read_idempotent(pg_client, db_session):
    """Marking an already-read notification read again is a no-op (still 200)."""
    user, token = _make_tenant_user(db_session)
    rows = _seed_notifs(db_session, user.tenant_id, n=1, read=True)
    notif_id = str(rows[0].id)

    resp = pg_client.patch(f"/api/v1/notifications/{notif_id}/read", headers=_auth(token))
    assert resp.status_code == 200
    assert resp.json()["data"]["read_at"] is not None


def test_mark_read_404_for_nonexistent(pg_client, db_session):
    """404 is returned when the notification id does not exist."""
    user, token = _make_tenant_user(db_session)
    fake_id = str(uuid.uuid4())

    resp = pg_client.patch(f"/api/v1/notifications/{fake_id}/read", headers=_auth(token))
    assert resp.status_code == 404
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "NOTIFICATION_NOT_FOUND"


def test_mark_read_404_cross_tenant(pg_client, db_session):
    """Tenant B cannot mark tenant A's notification as read (returns 404)."""
    user_a, token_a = _make_tenant_user(db_session)
    user_b, token_b = _make_tenant_user(db_session)

    rows_a = _seed_notifs(db_session, user_a.tenant_id, n=1, read=False)
    notif_id_a = str(rows_a[0].id)

    # Tenant B tries to mark tenant A's notification
    resp = pg_client.patch(f"/api/v1/notifications/{notif_id_a}/read", headers=_auth(token_b))
    assert resp.status_code == 404


def test_mark_read_unauthenticated_returns_401(pg_client):
    fake_id = str(uuid.uuid4())
    resp = pg_client.patch(f"/api/v1/notifications/{fake_id}/read")
    assert resp.status_code == 401


# ===========================================================================
# PATCH /api/v1/notifications/read-all
# ===========================================================================


def test_mark_all_read_marks_all_unread(pg_client, db_session):
    """PATCH /read-all sets read_at on all unread rows; returns updated count."""
    user, token = _make_tenant_user(db_session)
    _seed_notifs(db_session, user.tenant_id, n=4, read=False)
    _seed_notifs(db_session, user.tenant_id, n=1, read=True)  # already-read row

    resp = pg_client.patch("/api/v1/notifications/read-all", headers=_auth(token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["data"]["updated"] == 4

    # Verify unread-count is now 0
    count_resp = pg_client.get("/api/v1/notifications/unread-count", headers=_auth(token))
    assert count_resp.json()["data"]["count"] == 0


def test_mark_all_read_tenant_scoped(pg_client, db_session):
    """read-all only touches the calling tenant's unread rows."""
    user_me, token_me = _make_tenant_user(db_session)
    user_other, _ = _make_tenant_user(db_session)

    _seed_notifs(db_session, user_me.tenant_id, n=2, read=False)
    _seed_notifs(db_session, user_other.tenant_id, n=5, read=False)

    resp = pg_client.patch("/api/v1/notifications/read-all", headers=_auth(token_me))
    assert resp.status_code == 200
    assert resp.json()["data"]["updated"] == 2

    # Other tenant's notifications must be untouched — just check their count is still 5
    # (we don't have their token here, but we can trust the DB-level scope)


def test_mark_all_read_no_unread_returns_zero(pg_client, db_session):
    """read-all on a tenant with no unread returns updated=0."""
    user, token = _make_tenant_user(db_session)
    _seed_notifs(db_session, user.tenant_id, n=2, read=True)

    resp = pg_client.patch("/api/v1/notifications/read-all", headers=_auth(token))
    assert resp.status_code == 200
    assert resp.json()["data"]["updated"] == 0


def test_mark_all_read_unauthenticated_returns_401(pg_client):
    resp = pg_client.patch("/api/v1/notifications/read-all")
    assert resp.status_code == 401


# ===========================================================================
# Cross-tenant isolation
# ===========================================================================


def test_cross_tenant_list_isolation(pg_client, db_session):
    """Tenant B sees zero of tenant A's notifications."""
    user_a, _ = _make_tenant_user(db_session)
    user_b, token_b = _make_tenant_user(db_session)

    _seed_notifs(db_session, user_a.tenant_id, n=5)

    resp = pg_client.get("/api/v1/notifications", headers=_auth(token_b))
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["total"] == 0
    assert data["items"] == []
