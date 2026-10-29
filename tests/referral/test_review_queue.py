"""Platform-admin reward review queue, reward release/void and referral config endpoints.

Real Postgres (``db_session`` shared with the app through ``pg_client``). Fake data only; the
payout provider is never touched (the batch-detail test only reads).
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from loguru import logger
from sqlalchemy import select

from app.core.config import settings
from app.db.models.audit import AuditLog
from app.db.models.referral import (
    PayoutItem,
    PayoutItemStatus,
    ReferralConfig,
    ReferralReward,
    RewardStatus,
)
from app.db.models.tenant import Tenant
from app.services.referral import review_queue
from app.services.referral.config import get_referral_config
from app.services.referral.payouts import prepare_batch
from tests.referral.test_payouts import NOW, _marketer, _reward

BASE = "/api/v1/platform/referrals"
RS = RewardStatus


@pytest.fixture
def auth(super_admin_token):
    return {"Authorization": f"Bearer {super_admin_token}"}


@pytest.fixture
def plain_admin_headers(db_session):
    from app.core.security import create_access_token
    from app.db.models.platform_admin import PlatformAdmin, PlatformAdminRole

    admin = PlatformAdmin(
        email=f"plain_{uuid4().hex[:8]}@testdomain.com",
        hashed_password="x",
        full_name="Plain Admin",
        role=PlatformAdminRole.ADMIN,
        is_active=True,
    )
    db_session.add(admin)
    db_session.flush()
    token = create_access_token(
        subject=str(admin.id),
        additional_claims={"role": admin.role.value, "admin_type": "platform_admin"},
    )
    return {"Authorization": f"Bearer {token}"}


def _tenant(db, name="Queue Tenant", widget_host=None) -> Tenant:
    tenant = Tenant(
        name=f"t_{uuid4().hex[:8]}",
        display_name=name,
        api_key_hash=uuid4().hex,
        subscription_tier="trial",
        subscription_status="trialing",
        widget_last_seen_host=widget_host,
    )
    db.add(tenant)
    db.flush()
    return tenant


def _queued(
    db,
    marketer,
    status=RS.HELD,
    *,
    reasons=("shared_domain",),
    name="Queue Tenant",
    converted_at=None,
    amount=500_000,
    widget_host=None,
    tenant=True,
) -> ReferralReward:
    reward = ReferralReward(
        tenant_id=_tenant(db, name, widget_host).id if tenant else None,
        marketer_id=marketer.id,
        amount_kobo=amount,
        status=status,
        converted_at=converted_at or datetime.now(UTC),
        payable_after=(converted_at or datetime.now(UTC)) + timedelta(days=7),
        review_reasons=list(reasons),
    )
    db.add(reward)
    db.flush()
    return reward


ITEM_KEYS = {
    "id",
    "marketer_id",
    "marketer_name",
    "marketer_code",
    "tenant_company_name",
    "amount_kobo",
    "status",
    "review_reasons",
    "converted_at",
    "payable_after",
    "payout_batch_id",
    "shared_domain_matches",
}


# --- queue ---------------------------------------------------------------------------------------


def test_queue_defaults_to_held_newest_first_with_exact_item_keys(db_session, pg_client, auth):
    m = _marketer(db_session)
    old = _queued(db_session, m, converted_at=datetime.now(UTC) - timedelta(days=3), name="Old Co")
    new = _queued(db_session, m, converted_at=datetime.now(UTC) - timedelta(days=1), name="New Co")
    _queued(db_session, m, RS.PENDING, reasons=[], name="Not Held Co")

    resp = pg_client.get(f"{BASE}/rewards", headers=auth)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"items", "total", "limit", "offset"}
    assert body["limit"] == 50 and body["offset"] == 0
    ids = [i["id"] for i in body["items"]]
    assert ids.index(str(new.id)) < ids.index(str(old.id))
    assert all(i["status"] == "held" for i in body["items"])
    item = next(i for i in body["items"] if i["id"] == str(new.id))
    assert set(item) == ITEM_KEYS
    assert item["marketer_id"] == str(m.id)
    assert item["marketer_name"] == m.name and item["marketer_code"] == m.code
    assert item["tenant_company_name"] == "New Co"
    assert item["amount_kobo"] == 500_000
    assert item["review_reasons"] == ["shared_domain"]
    assert item["payout_batch_id"] is None


def test_queue_never_leaks_marketer_secrets(db_session, pg_client, auth):
    m = _marketer(db_session)
    m.password_hash = "bcrypt-hash-leak-check"
    db_session.flush()
    _queued(db_session, m)
    resp = pg_client.get(f"{BASE}/rewards?status=all", headers=auth)
    for needle in ("0000000000", "bcrypt-hash-leak-check", "account_number", "password", m.email):
        assert needle not in resp.text


@pytest.mark.parametrize("status", ["pending", "held", "payable", "batched", "paid", "void"])
def test_queue_status_filter(db_session, pg_client, auth, status):
    m = _marketer(db_session)
    rewards = {s: _queued(db_session, m, s, reasons=[]) for s in RS}
    resp = pg_client.get(f"{BASE}/rewards?status={status}", headers=auth)
    assert resp.status_code == 200, resp.text
    ids = {i["id"] for i in resp.json()["items"]}
    assert str(rewards[RS(status)].id) in ids
    assert all(i["status"] == status for i in resp.json()["items"])


def test_queue_status_all_and_invalid_status(db_session, pg_client, auth):
    m = _marketer(db_session)
    rewards = [_queued(db_session, m, s, reasons=[]) for s in RS]
    resp = pg_client.get(f"{BASE}/rewards?status=all", headers=auth)
    assert {str(r.id) for r in rewards} <= {i["id"] for i in resp.json()["items"]}
    assert pg_client.get(f"{BASE}/rewards?status=nonsense", headers=auth).status_code == 422


def test_queue_pagination_is_stable_and_bounded(db_session, pg_client, auth):
    m = _marketer(db_session)
    base = datetime.now(UTC) - timedelta(days=30)
    made = [_queued(db_session, m, converted_at=base + timedelta(hours=i)) for i in range(5)]
    expected = [str(r.id) for r in reversed(made)]

    first = pg_client.get(f"{BASE}/rewards?limit=2&offset=0", headers=auth).json()
    second = pg_client.get(f"{BASE}/rewards?limit=2&offset=2", headers=auth).json()

    assert first["total"] >= 5 and first["limit"] == 2 and second["offset"] == 2
    got = [i["id"] for i in first["items"]] + [i["id"] for i in second["items"]]
    assert got == expected[:4]
    for qs in ("limit=0", "limit=201", "offset=-1"):
        assert pg_client.get(f"{BASE}/rewards?{qs}", headers=auth).status_code == 422


def test_queue_tenant_name_is_none_when_the_tenant_was_purged(db_session, pg_client, auth):
    m = _marketer(db_session)
    reward = _queued(db_session, m, tenant=False)
    resp = pg_client.get(f"{BASE}/rewards", headers=auth)
    item = next(i for i in resp.json()["items"] if i["id"] == str(reward.id))
    assert item["tenant_company_name"] is None
    assert item["shared_domain_matches"] == []


def test_shared_domain_matches_shape_and_only_for_shared_domain_reason(db_session, pg_client, auth):
    m = _marketer(db_session)
    flagged = _queued(db_session, m, name="Me Ltd", widget_host="shop.example.com")
    _tenant(db_session, "Zed Stores", widget_host="www.example.com")
    other = _queued(db_session, m, reasons=["shared_card"], name="Card Co")

    items = {i["id"]: i for i in pg_client.get(f"{BASE}/rewards", headers=auth).json()["items"]}

    matches = items[str(flagged.id)]["shared_domain_matches"]
    assert matches == [
        {
            "other_tenant_name": "Zed Stores",
            "domain": "example.com",
            "sources": ["widget"],
            "own_sources": ["widget"],
        }
    ]
    assert set(matches[0]) == {"other_tenant_name", "domain", "sources", "own_sources"}
    assert items[str(other.id)]["shared_domain_matches"] == []


def test_one_failing_reward_does_not_break_the_page(db_session, pg_client, auth, monkeypatch):
    m = _marketer(db_session)
    good = _queued(db_session, m, name="Good Co")
    bad = _queued(db_session, m, name="Bad Co")
    calls = []

    def flaky(db, tenant_id):
        calls.append(tenant_id)
        if tenant_id == bad.tenant_id:
            raise RuntimeError("secret internals tenant-leak-check")
        return []

    monkeypatch.setattr(review_queue, "shared_domain_tenants", flaky)
    logged = []
    sink = logger.add(lambda message: logged.append(str(message)), level="ERROR")
    try:
        resp = pg_client.get(f"{BASE}/rewards", headers=auth)
    finally:
        logger.remove(sink)

    assert resp.status_code == 200, resp.text
    items = {i["id"]: i for i in resp.json()["items"]}
    assert items[str(bad.id)]["shared_domain_matches"] == []
    assert items[str(good.id)]["shared_domain_matches"] == []
    assert {good.tenant_id, bad.tenant_id} <= set(calls)
    text = " ".join(logged)
    assert "RuntimeError" in text and str(bad.id) in text
    assert "tenant-leak-check" not in text  # class and ids only, never the exception text


def test_queue_only_computes_matches_for_the_page(db_session, pg_client, auth, monkeypatch):
    m = _marketer(db_session)
    base = datetime.now(UTC) - timedelta(days=30)
    for i in range(4):
        _queued(db_session, m, converted_at=base + timedelta(hours=i))
    seen = []
    monkeypatch.setattr(
        review_queue, "shared_domain_tenants", lambda db, tenant_id: seen.append(tenant_id) or []
    )
    pg_client.get(f"{BASE}/rewards?limit=2", headers=auth)
    assert len(seen) == 2


# --- release / void --------------------------------------------------------------------------------


def _act(client, headers, reward_id, action, note="checked the evidence"):
    kw = {"headers": headers} if headers else {}
    return client.post(f"{BASE}/rewards/{reward_id}/{action}", json={"note": note}, **kw)


def test_release_happy_path_pending_or_payable_and_audited(db_session, pg_client, auth):
    m = _marketer(db_session)
    recent = _queued(db_session, m)  # hold date still ahead -> pending
    old = _queued(db_session, m, converted_at=datetime.now(UTC) - timedelta(days=30))

    r1 = _act(pg_client, auth, recent.id, "release")
    r2 = _act(pg_client, auth, old.id, "release")

    assert r1.status_code == 200, r1.text
    assert r1.json() == {"id": str(recent.id), "status": "pending"}
    assert r2.json() == {"id": str(old.id), "status": "payable"}
    entry = db_session.execute(
        select(AuditLog).where(AuditLog.resource_id == recent.id)
    ).scalar_one()
    assert entry.action == "release_referral_reward"
    assert entry.resource_type == "referral_reward"


def test_void_happy_path_audited(db_session, pg_client, auth):
    m = _marketer(db_session)
    reward = _queued(db_session, m)
    resp = _act(pg_client, auth, reward.id, "void")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"id": str(reward.id), "status": "void"}
    entry = db_session.execute(
        select(AuditLog).where(AuditLog.resource_id == reward.id)
    ).scalar_one()
    assert entry.action == "void_referral_reward"


@pytest.mark.parametrize("action", ["release", "void"])
@pytest.mark.parametrize("note", ["", "   ", None])
def test_release_and_void_require_note(db_session, pg_client, auth, action, note):
    m = _marketer(db_session)
    reward = _queued(db_session, m)
    body = {} if note is None else {"note": note}
    resp = pg_client.post(f"{BASE}/rewards/{reward.id}/{action}", json=body, headers=auth)
    assert resp.status_code == 422
    db_session.expire_all()
    assert db_session.get(ReferralReward, reward.id).status == RS.HELD


def test_release_and_void_reject_unknown_fields(db_session, pg_client, auth):
    m = _marketer(db_session)
    reward = _queued(db_session, m)
    resp = pg_client.post(
        f"{BASE}/rewards/{reward.id}/void", json={"note": "ok", "status": "paid"}, headers=auth
    )
    assert resp.status_code == 422


def test_release_and_void_role_matrix(
    db_session, pg_client, auth, plain_admin_headers, regular_user_token
):
    m = _marketer(db_session)
    reward = _queued(db_session, m)
    tenant_headers = {"Authorization": f"Bearer {regular_user_token}"}

    for action in ("release", "void"):
        assert _act(pg_client, None, reward.id, action).status_code == 401
        assert _act(pg_client, tenant_headers, reward.id, action).status_code == 401
        assert _act(pg_client, plain_admin_headers, reward.id, action).status_code == 403
    db_session.expire_all()
    assert db_session.get(ReferralReward, reward.id).status == RS.HELD

    assert _act(pg_client, auth, reward.id, "release").status_code == 200


def test_queue_and_config_reads_allow_any_platform_admin_but_not_tenants(
    db_session, pg_client, plain_admin_headers, regular_user_token
):
    tenant_headers = {"Authorization": f"Bearer {regular_user_token}"}
    for path in ("/rewards", "/config"):
        assert pg_client.get(f"{BASE}{path}").status_code == 401
        assert pg_client.get(f"{BASE}{path}", headers=tenant_headers).status_code == 401
        assert pg_client.get(f"{BASE}{path}", headers=plain_admin_headers).status_code == 200


def test_unknown_reward_is_404_with_a_fixed_body(db_session, pg_client, auth):
    resp = _act(pg_client, auth, uuid4(), "release")
    assert resp.status_code == 404
    assert resp.json() == {"detail": {"code": "reward_not_found", "message": "Reward not found"}}
    assert _act(pg_client, auth, uuid4(), "void").status_code == 404


def test_wrong_status_is_409_with_fixed_message(db_session, pg_client, auth):
    m = _marketer(db_session)
    pending = _queued(db_session, m, RS.PENDING, reasons=[])
    paid = _queued(db_session, m, RS.PAID, reasons=[])

    r1 = _act(pg_client, auth, pending.id, "release")
    r2 = _act(pg_client, auth, paid.id, "void")

    for resp in (r1, r2):
        assert resp.status_code == 409, resp.text
        detail = resp.json()["detail"]
        assert detail["code"] == "reward_state_invalid"
        assert str(pending.id) not in resp.text and str(paid.id) not in resp.text
        assert "only held" not in resp.text  # no str(exc)


def test_reward_in_flight_is_409_reward_in_flight(db_session, pg_client, auth):
    batch_marketer = _marketer(db_session)
    reward = _reward(db_session, batch_marketer, RS.PAYABLE)
    batch = prepare_batch(db_session, NOW)
    assert batch is not None
    row = db_session.get(ReferralReward, reward.id)
    row.status = RS.HELD  # held after the claim, still tied to the batch
    db_session.flush()

    for action in ("release", "void"):
        resp = _act(pg_client, auth, reward.id, action)
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"]["code"] == "reward_in_flight"
        assert str(batch.id) not in resp.text
    db_session.expire_all()
    assert db_session.get(ReferralReward, reward.id).status == RS.HELD


# --- config ---------------------------------------------------------------------------------------


def test_get_config_reports_env_defaults_and_sources(db_session, pg_client, auth):
    resp = pg_client.get(f"{BASE}/config", headers=auth)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "reward_kobo": settings.REFERRAL_REWARD_KOBO,
        "hold_days": settings.REFERRAL_HOLD_DAYS,
        "commission_window_days": settings.REFERRAL_COMMISSION_WINDOW_DAYS,
        "source_by_key": {
            "reward_kobo": "env",
            "hold_days": "env",
            "commission_window_days": "env",
        },
    }


def test_patch_config_persists_and_second_read_comes_from_the_db(
    db_session, pg_client, auth, super_admin_user
):
    resp = pg_client.patch(
        f"{BASE}/config", json={"reward_kobo": 750_000, "hold_days": 10}, headers=auth
    )

    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "reward_kobo": 750_000,
        "hold_days": 10,
        "commission_window_days": settings.REFERRAL_COMMISSION_WINDOW_DAYS,
        "source_by_key": {
            "reward_kobo": "db",
            "hold_days": "db",
            "commission_window_days": "env",
        },
    }
    again = pg_client.get(f"{BASE}/config", headers=auth).json()
    assert again["reward_kobo"] == 750_000 and again["source_by_key"]["hold_days"] == "db"
    effective = get_referral_config(db_session)
    assert (effective.reward_kobo, effective.hold_days) == (750_000, 10)
    rows = {r.key: r for r in db_session.execute(select(ReferralConfig)).scalars()}
    assert rows["reward_kobo"].value == 750_000
    assert rows["reward_kobo"].updated_by_admin_id == super_admin_user.id

    # a second patch updates the existing row and leaves the other keys alone
    pg_client.patch(f"{BASE}/config", json={"reward_kobo": 600_000}, headers=auth)
    db_session.expire_all()
    effective = get_referral_config(db_session)
    assert (effective.reward_kobo, effective.hold_days) == (600_000, 10)


def test_patch_config_is_audited_with_keys_and_values_only(db_session, pg_client, auth):
    pg_client.patch(f"{BASE}/config", json={"commission_window_days": 120}, headers=auth)
    entries = list(
        db_session.execute(
            select(AuditLog).where(AuditLog.action == "update_referral_config")
        ).scalars()
    )
    assert len(entries) == 1
    metadata = entries[0].changes["metadata"]
    assert metadata == {"changed": {"commission_window_days": 120}}


def test_existing_rewards_keep_their_snapshot_amount(db_session, pg_client, auth):
    m = _marketer(db_session)
    reward = _queued(db_session, m, amount=500_000)
    pg_client.patch(f"{BASE}/config", json={"reward_kobo": 900_000}, headers=auth)
    db_session.expire_all()
    assert db_session.get(ReferralReward, reward.id).amount_kobo == 500_000


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"reward_kobo": 9_999},
        {"reward_kobo": 5_000_001},
        {"hold_days": -1},
        {"hold_days": 61},
        {"commission_window_days": 0},
        {"commission_window_days": 366},
        {"reward_kobo": 500_000.5},
        {"reward_kobo": "500000"},
        {"reward_kobo": True},
        {"reward_kobo": 500_000.0},
        {"hold_days": None},
        {"reward_kobo": 500_000, "surprise": 1},
        {"batch_days": 3},
    ],
)
def test_config_patch_validates_ranges(db_session, pg_client, auth, body):
    resp = pg_client.patch(f"{BASE}/config", json=body, headers=auth)
    assert resp.status_code == 422, resp.text
    assert db_session.execute(select(ReferralConfig)).first() is None


@pytest.mark.parametrize(
    "body",
    [
        {"reward_kobo": 10_000},
        {"reward_kobo": 5_000_000},
        {"hold_days": 0},
        {"hold_days": 60},
        {"commission_window_days": 1},
        {"commission_window_days": 365},
    ],
)
def test_config_patch_accepts_the_bounds(db_session, pg_client, auth, body):
    assert pg_client.patch(f"{BASE}/config", json=body, headers=auth).status_code == 200


def test_config_patch_role_matrix(
    db_session, pg_client, auth, plain_admin_headers, regular_user_token
):
    body = {"hold_days": 5}
    tenant_headers = {"Authorization": f"Bearer {regular_user_token}"}
    assert pg_client.patch(f"{BASE}/config", json=body).status_code == 401
    assert pg_client.patch(f"{BASE}/config", json=body, headers=tenant_headers).status_code == 401
    assert (
        pg_client.patch(f"{BASE}/config", json=body, headers=plain_admin_headers).status_code == 403
    )
    assert db_session.execute(select(ReferralConfig)).first() is None
    assert pg_client.patch(f"{BASE}/config", json=body, headers=auth).status_code == 200


def test_literal_routes_are_not_shadowed_by_the_marketer_id_routes(pg_client, auth):
    # /rewards and /config would hit a 422 (uuid parse) if {marketer_id:uuid} captured them.
    assert pg_client.get(f"{BASE}/rewards", headers=auth).status_code == 200
    assert pg_client.get(f"{BASE}/config", headers=auth).status_code == 200


# --- batch detail: current_amount_kobo -------------------------------------------------------------


def test_batch_detail_current_amount_excludes_a_held_but_linked_reward(db_session, pg_client, auth):
    marketer = _marketer(db_session)
    kept = _reward(db_session, marketer, RS.PAYABLE, amount=300_000)
    held = _reward(db_session, marketer, RS.PAYABLE, amount=200_000)
    batch = prepare_batch(db_session, NOW)
    assert batch is not None
    item = db_session.scalar(select(PayoutItem).where(PayoutItem.batch_id == batch.id))
    item.status = PayoutItemStatus.FAILED
    held_row = db_session.get(ReferralReward, held.id)
    held_row.status = RS.HELD  # held since the claim; still linked to the batch
    db_session.flush()
    assert db_session.get(ReferralReward, kept.id).status == RS.BATCHED

    resp = pg_client.get(f"{BASE}/batches/{batch.id}", headers=auth)

    assert resp.status_code == 200, resp.text
    row = resp.json()["items"][0]
    assert row["amount_kobo"] == 500_000  # what was claimed
    assert row["current_amount_kobo"] == 300_000  # what mark-paid would accept now
    assert row["status"] == "failed"


def test_batch_detail_current_amount_matches_amount_for_a_plain_item(db_session, pg_client, auth):
    marketer = _marketer(db_session)
    _reward(db_session, marketer, RS.PAYABLE, amount=500_000)
    _reward(db_session, marketer, RS.PAYABLE, amount=250_000)
    batch = prepare_batch(db_session, NOW)
    row = pg_client.get(f"{BASE}/batches/{batch.id}", headers=auth).json()["items"][0]
    assert row["amount_kobo"] == row["current_amount_kobo"] == 750_000
