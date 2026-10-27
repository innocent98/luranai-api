"""Widget-seen tracking on GET /api/v1/widget/public/config/{tenant_id}."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.core.config import settings
from app.db.models.tenant import Tenant
from app.services.widget_seen_service import (
    WIDGET_SEEN_THROTTLE,
    extract_hostname,
    record_widget_seen,
)
from tests.api.v1 import test_chat_image_send as img


def _fetch(client, tenant_id, key_tenant_id=None, **headers):
    key = SimpleNamespace(tenant_id=key_tenant_id or tenant_id)
    with patch("app.api.v1.endpoints.widget.APIKeyService.verify_api_key", return_value=key):
        return client.get(
            f"/api/v1/widget/public/config/{tenant_id}",
            headers={"X-API-Key": "pk_live_test", **headers},
        )


def _tenant(db, tenant_id) -> Tenant:
    db.expire_all()
    return db.get(Tenant, tenant_id)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://Shop.Example.com", "shop.example.com"),
        ("https://shop.example.com:8443/a/b?token=secret#frag", "shop.example.com"),
        ("https://user:pw@shop.example.com/", "shop.example.com"),
        ("http://localhost:3000", "localhost"),
        ("https://shop.example.com./", "shop.example.com"),
        ("null", None),
        ("", None),
        ("not a url", None),
        ("https://exa mple.com", None),
        ("https://[::1]:8000/", None),
        ("https://" + "a" * 300 + ".com", None),
    ],
)
def test_extract_hostname_keeps_only_a_clean_hostname(raw, expected):
    assert extract_hostname(raw) == expected


def test_extract_hostname_falls_back_to_referer():
    assert extract_hostname("null", "https://shop.example.com/pricing?a=1") == "shop.example.com"
    assert extract_hostname(None, None) is None


def test_fetch_records_hostname_only(client, db):
    _, tid = img._session(db)
    r = _fetch(
        client,
        tid,
        Origin="https://Shop.Example.com:8443",
        Referer="https://shop.example.com/secret/path?email=a@b.c",
    )
    assert r.status_code == 200, r.text
    t = _tenant(db, tid)
    assert t.widget_last_seen_host == "shop.example.com"
    assert t.widget_first_seen_at is not None
    assert t.widget_first_seen_at == t.widget_last_seen_at
    stored = f"{t.widget_last_seen_host}"
    for leak in ("secret", "email", "8443", "https", "/"):
        assert leak not in stored


def test_response_is_unchanged_by_tracking(client, db):
    _, tid = img._session(db)
    with_origin = _fetch(client, tid, Origin="https://shop.example.com")
    without = _fetch(client, tid)
    assert with_origin.status_code == without.status_code == 200
    assert with_origin.json() == without.json()


def test_no_origin_or_referer_records_nothing(client, db):
    _, tid = img._session(db)
    assert _fetch(client, tid).status_code == 200
    t = _tenant(db, tid)
    assert t.widget_first_seen_at is None and t.widget_last_seen_host is None


def test_referer_is_used_when_origin_is_missing(client, db):
    _, tid = img._session(db)
    _fetch(client, tid, Referer="https://blog.example.org/post?id=1")
    assert _tenant(db, tid).widget_last_seen_host == "blog.example.org"


def test_second_fetch_within_ten_minutes_does_not_write(client, db):
    _, tid = img._session(db)
    _fetch(client, tid, Origin="https://one.example.com")
    first = _tenant(db, tid).widget_last_seen_at
    _fetch(client, tid, Origin="https://two.example.com")
    t = _tenant(db, tid)
    assert t.widget_last_seen_at == first
    assert t.widget_last_seen_host == "one.example.com"


def test_fetch_after_the_window_updates_last_but_keeps_first(client, db):
    _, tid = img._session(db)
    _fetch(client, tid, Origin="https://one.example.com")
    t = _tenant(db, tid)
    first = t.widget_first_seen_at
    t.widget_last_seen_at = datetime.now(UTC) - WIDGET_SEEN_THROTTLE - timedelta(minutes=1)
    db.commit()
    _fetch(client, tid, Origin="https://two.example.com")
    t = _tenant(db, tid)
    assert t.widget_last_seen_host == "two.example.com"
    assert t.widget_first_seen_at == first
    assert t.widget_last_seen_at > first


def test_throttle_is_enforced_by_the_update_itself(db):
    """A stale in-memory row (concurrent request) must not cause a second write."""
    _, tid = img._session(db)
    t = _tenant(db, tid)
    now = datetime.now(UTC)
    assert record_widget_seen(db, t, "https://one.example.com", None, now=now) is True
    stale = _tenant(db, tid)
    stale.widget_last_seen_at = None  # what a concurrent request would still be holding
    assert record_widget_seen(db, stale, "https://two.example.com", None, now=now) is False
    assert _tenant(db, tid).widget_last_seen_host == "one.example.com"


def test_dashboard_origin_is_ignored(client, db, monkeypatch):
    _, tid = img._session(db)
    monkeypatch.setattr(settings, "FRONTEND_URL", "https://dashboard.luran.example")
    _fetch(client, tid, Origin="https://dashboard.luran.example")
    assert _tenant(db, tid).widget_first_seen_at is None


def test_configured_hosts_and_wildcards_are_ignored(client, db, monkeypatch):
    _, tid = img._session(db)
    monkeypatch.setattr(
        settings, "WIDGET_SEEN_IGNORED_HOSTS", "preview.luran.example, *.vercel-preview.example"
    )
    _fetch(client, tid, Origin="https://preview.luran.example")
    _fetch(client, tid, Origin="https://pr-12.vercel-preview.example")
    assert _tenant(db, tid).widget_first_seen_at is None
    _fetch(client, tid, Origin="https://shop.example.com")
    assert _tenant(db, tid).widget_last_seen_host == "shop.example.com"


def test_another_tenants_key_cannot_stamp_this_tenant(client, db):
    _, tid_a = img._session(db)
    _, tid_b = img._session(db)
    r = _fetch(client, tid_a, key_tenant_id=tid_b, Origin="https://evil.example.com")
    assert r.status_code == 403
    assert _tenant(db, tid_a).widget_first_seen_at is None
    assert _tenant(db, tid_b).widget_first_seen_at is None


def test_only_the_fetching_tenant_is_written(client, db):
    _, tid_a = img._session(db)
    _, tid_b = img._session(db)
    _fetch(client, tid_a, Origin="https://a.example.com")
    assert _tenant(db, tid_a).widget_last_seen_host == "a.example.com"
    assert _tenant(db, tid_b).widget_last_seen_host is None


def test_tracking_failure_is_swallowed_and_rolled_back():
    """A DB error while stamping must not break the config response."""
    from unittest.mock import MagicMock

    db = MagicMock()
    db.execute.side_effect = RuntimeError("db down")
    tenant = SimpleNamespace(id="t", widget_last_seen_at=None)
    assert record_widget_seen(db, tenant, "https://shop.example.com", None) is False
    db.rollback.assert_called_once()
