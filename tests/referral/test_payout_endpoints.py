"""Platform-admin payout batch endpoints (Task 8 Part B).

Real Postgres (``db_session`` shared with the app through ``pg_client``). The payout provider is a
fake injected by replacing ``payouts.get_payout_provider`` - NO test makes an HTTP call, and the
bank details are obviously fake placeholders.
"""

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from loguru import logger
from sqlalchemy import func, select

from app.db.models.audit import AuditLog
from app.db.models.referral import (
    BatchStatus,
    Marketer,
    MarketerStatus,
    PayoutBatch,
    PayoutItemStatus,
    ReferralReward,
    RewardStatus,
)
from app.services.enhanced_audit_service import EnhancedAuditService
from app.services.referral import payouts
from app.services.referral.payouts import prepare_batch
from app.services.referral.providers import (
    PayoutProviderError,
    TransferResult,
    TransferVerification,
)
from tests.referral.test_payouts import (
    NOW,
    FakeProvider,
    _batch_of,
    _failing_for,
    _item,
    _marketer,
    _reward,
    _verify_as,
)

BASE = "/api/v1/platform/referrals"
LEAK_ACCOUNT = "0098765432"
LEAK_RECIPIENT = "RCP_leakcheck_9x"
LEAK_NAME = "LEAK CHECK ACCOUNT NAME"
PS = PayoutItemStatus


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


@pytest.fixture
def use_provider(monkeypatch):
    """Install a fake provider as the one every payout function resolves."""

    def install(provider):
        monkeypatch.setattr(payouts, "get_payout_provider", lambda db=None: provider)
        return provider

    return install


def _manual_result(marketer, amount, reference):
    return TransferResult("manual", reference, "manual")


def _approve(client, auth, batch_id):
    return client.post(f"{BASE}/batches/{batch_id}/approve", headers=auth)


def _mark_paid(client, auth, batch_id, entries):
    body = {
        "items": [
            {
                "marketer_id": str(e[0]),
                "bank_reference": e[1],
                "amount_kobo": e[2] if len(e) > 2 else 500_000,
            }
            for e in entries
        ]
    }
    return client.post(f"{BASE}/batches/{batch_id}/mark-paid", json=body, headers=auth)


def _no_leak(resp):
    text = resp.text
    for needle in (
        LEAK_ACCOUNT,
        LEAK_RECIPIENT,
        LEAK_NAME,
        "account_number",
        "account_name",
        "bank_code",
        "recipient_code",
        "password",
    ):
        assert needle not in text, f"{needle!r} leaked in {resp.request.url.path}"


# --- auth matrix -----------------------------------------------------------------------------


def _all_calls(client, headers, batch_id):
    kw = {"headers": headers} if headers else {}
    return [
        client.get(f"{BASE}/batches", **kw),
        client.get(f"{BASE}/batches/{batch_id}", **kw),
        client.post(f"{BASE}/batches/{batch_id}/approve", **kw),
        client.post(f"{BASE}/batches/{batch_id}/retry", **kw),
        client.post(
            f"{BASE}/batches/{batch_id}/mark-paid",
            json={
                "items": [
                    {"marketer_id": str(uuid4()), "bank_reference": "R1", "amount_kobo": 500000}
                ]
            },
            **kw,
        ),
        client.post(f"{BASE}/batches/prepare", **kw),
    ]


def test_no_token_and_tenant_token_are_401(db_session, pg_client, regular_user_token, use_provider):
    provider = use_provider(FakeProvider())
    batch, _, _ = _batch_of(db_session)
    tenant_headers = {"Authorization": f"Bearer {regular_user_token}"}

    assert [r.status_code for r in _all_calls(pg_client, None, batch.id)] == [401] * 6
    assert [r.status_code for r in _all_calls(pg_client, tenant_headers, batch.id)] == [401] * 6
    assert provider.pay_calls == []


def test_plain_admin_reads_but_cannot_write(
    db_session, pg_client, plain_admin_headers, use_provider
):
    provider = use_provider(FakeProvider())
    batch, _, _ = _batch_of(db_session)

    got = _all_calls(pg_client, plain_admin_headers, batch.id)

    assert [r.status_code for r in got] == [200, 200, 403, 403, 403, 403]
    assert provider.pay_calls == [] and provider.verify_calls == []
    db_session.expire_all()
    assert db_session.get(PayoutBatch, batch.id).status == BatchStatus.DRAFT


def test_super_admin_passes_the_role_gate(db_session, pg_client, auth, use_provider):
    use_provider(FakeProvider())
    batch, _, _ = _batch_of(db_session)
    got = _all_calls(pg_client, auth, batch.id)
    # approve ok; retry on a now-paid batch is a 200 no-op; mark-paid on a paid batch is a 409;
    # prepare has nothing payable (or prepares a new draft): 200 either way
    assert [r.status_code for r in got] == [200, 200, 200, 200, 409, 200]


# --- read shapes -----------------------------------------------------------------------------


def test_list_batches_newest_first_with_counts_and_pagination(db_session, pg_client, auth):
    older, _, _ = _batch_of(db_session, count=1, amount=100_000)
    older_row = db_session.get(PayoutBatch, older.id)
    older_row.created_at = datetime.now(UTC) - timedelta(days=15)
    db_session.flush()
    newer, _, _ = _batch_of(db_session, count=3, amount=200_000)

    resp = pg_client.get(f"{BASE}/batches", headers=auth)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"items", "total", "limit", "offset"}
    assert body["total"] >= 2 and body["limit"] == 50 and body["offset"] == 0
    ids = [b["id"] for b in body["items"]]
    assert ids.index(str(newer.id)) < ids.index(str(older.id))
    row = next(b for b in body["items"] if b["id"] == str(newer.id))
    assert set(row) == {
        "id",
        "period_label",
        "status",
        "total_kobo",
        "item_counts",
        "approved_at",
        "approved_by_admin_id",
        "created_at",
    }
    assert row["status"] == "draft"
    assert row["total_kobo"] == 600_000
    assert row["item_counts"] == {
        "pending": 3,
        "processing": 0,
        "paid": 0,
        "failed": 0,
        "skipped": 0,
        "awaiting_manual": 0,
    }
    assert row["approved_at"] is None and row["approved_by_admin_id"] is None

    page = pg_client.get(f"{BASE}/batches?limit=1&offset=0", headers=auth).json()
    assert len(page["items"]) == 1 and page["limit"] == 1
    assert pg_client.get(f"{BASE}/batches?limit=0", headers=auth).status_code == 422
    assert pg_client.get(f"{BASE}/batches?limit=201", headers=auth).status_code == 422
    assert pg_client.get(f"{BASE}/batches?offset=-1", headers=auth).status_code == 422


def test_batch_detail_items_and_held_rewards_left_out(db_session, pg_client, auth):
    batch, (a, b), (ra, rb) = _batch_of(db_session, count=2)
    # b also has a held reward that prepare_batch left out; a has one that was never in the batch
    _reward(db_session, b, RewardStatus.HELD)
    _reward(db_session, b, RewardStatus.HELD)
    other_batch_reward = _reward(db_session, a, RewardStatus.PENDING)
    assert other_batch_reward.status == RewardStatus.PENDING

    resp = pg_client.get(f"{BASE}/batches/{batch.id}", headers=auth)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == str(batch.id) and body["status"] == "draft"
    assert body["total_kobo"] == 1_000_000
    assert len(body["items"]) == 2
    by_marketer = {i["marketer_id"]: i for i in body["items"]}
    item_b = by_marketer[str(b.id)]
    assert set(item_b) == {
        "id",
        "marketer_id",
        "marketer_name",
        "marketer_code",
        "amount_kobo",
        "current_amount_kobo",
        "status",
        "failure_reason",
        "provider_status",
        "attempt",
        "transfer_reference",
        "reward_count",
        "reward_ids",
        "bank_last4",
        "held_rewards_excluded",
    }
    assert item_b["marketer_name"] == "Payout Marketer"
    assert item_b["marketer_code"] == b.code
    assert item_b["amount_kobo"] == 500_000
    assert item_b["status"] == "pending"
    assert item_b["attempt"] == 1
    assert item_b["transfer_reference"] is None
    assert item_b["reward_count"] == 1 and item_b["reward_ids"] == [str(rb.id)]
    assert item_b["bank_last4"] == "0000"
    assert item_b["held_rewards_excluded"] == 2
    assert by_marketer[str(a.id)]["held_rewards_excluded"] == 0
    _no_leak(resp)


def test_unknown_batch_is_404_batch_not_found_on_every_route(db_session, pg_client, auth):
    unknown = uuid4()
    calls = [
        pg_client.get(f"{BASE}/batches/{unknown}", headers=auth),
        pg_client.post(f"{BASE}/batches/{unknown}/approve", headers=auth),
        pg_client.post(f"{BASE}/batches/{unknown}/retry", headers=auth),
        pg_client.post(
            f"{BASE}/batches/{unknown}/mark-paid",
            json={
                "items": [
                    {"marketer_id": str(uuid4()), "bank_reference": "R1", "amount_kobo": 500000}
                ]
            },
            headers=auth,
        ),
    ]
    for resp in calls:
        assert resp.status_code == 404, resp.text
        assert resp.json()["detail"]["code"] == "batch_not_found"


def test_literal_batch_routes_are_not_shadowed_by_the_marketer_routes(db_session, pg_client, auth):
    batch, (a, _), _ = _batch_of(db_session)

    listing = pg_client.get(f"{BASE}/batches", headers=auth)
    assert listing.status_code == 200 and "items" in listing.json()
    assert listing.json()["items"][0].get("period_label")  # a batch row, not a marketer row

    detail = pg_client.get(f"{BASE}/batches/{batch.id}", headers=auth)
    assert detail.status_code == 200 and detail.json()["id"] == str(batch.id)

    # the marketer routes still resolve a uuid to a marketer
    marketer = pg_client.get(f"{BASE}/{a.id}", headers=auth)
    assert marketer.status_code == 200 and marketer.json()["id"] == str(a.id)
    assert pg_client.get(f"{BASE}/{uuid4()}", headers=auth).json()["detail"]["code"] == (
        "marketer_not_found"
    )
    # and a batch id is not a marketer id
    assert pg_client.get(f"{BASE}/{batch.id}", headers=auth).status_code == 404


# --- approve ---------------------------------------------------------------------------------


def test_approve_pays_every_item_and_a_second_approve_changes_nothing(
    db_session, pg_client, auth, use_provider, super_admin_user
):
    provider = use_provider(FakeProvider())
    batch, (a, b), rewards = _batch_of(db_session)

    first = _approve(pg_client, auth, batch.id)
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["status"] == "paid"
    assert body["approved_by_admin_id"] == str(super_admin_user.id)
    assert body["approved_at"] is not None
    assert {i["status"] for i in body["items"]} == {"paid"}
    assert all(i["transfer_reference"].startswith(f"ref-{batch.id}-") for i in body["items"])
    assert len(provider.pay_calls) == 2

    second = _approve(pg_client, auth, batch.id)
    assert second.status_code == 200, second.text
    assert second.json() == body
    assert len(provider.pay_calls) == 2  # still one call per item
    db_session.expire_all()
    assert all(db_session.get(ReferralReward, r.id).status == RewardStatus.PAID for r in rewards)


def test_approve_with_a_manual_provider_leaves_items_awaiting_a_human(
    db_session, pg_client, auth, use_provider
):
    use_provider(FakeProvider(pay_fn=_manual_result))
    batch, _, _ = _batch_of(db_session)

    body = _approve(pg_client, auth, batch.id).json()

    assert body["status"] == "paying"
    assert {i["status"] for i in body["items"]} == {"awaiting_manual"}


# --- retry -----------------------------------------------------------------------------------


def test_retry_pays_only_the_failed_item_and_keeps_paid_ones(
    db_session, pg_client, auth, use_provider
):
    batch, (a, b), _ = _batch_of(db_session)
    provider = use_provider(FakeProvider(pay_fn=_failing_for(b.id), verify_fn=_verify_as("failed")))

    first = _approve(pg_client, auth, batch.id).json()
    assert first["status"] == "failed"
    statuses = {i["marketer_id"]: i["status"] for i in first["items"]}
    assert statuses == {str(a.id): "paid", str(b.id): "failed"}
    failed_item = next(i for i in first["items"] if i["marketer_id"] == str(b.id))
    assert failed_item["failure_reason"] == "insufficient balance"
    calls_before = len(provider.pay_calls)

    # the provider now accepts everything
    provider._pay_fn = lambda m, amt, ref: TransferResult("success", ref, "success")
    retried = pg_client.post(f"{BASE}/batches/{batch.id}/retry", headers=auth)

    assert retried.status_code == 200, retried.text
    body = retried.json()
    assert body["status"] == "paid"
    assert len(provider.pay_calls) == calls_before + 1  # only the failed item was sent again
    assert provider.pay_calls[-1][0] == b.id
    assert provider.pay_calls[-1][2].endswith("-r2")
    item_b = next(i for i in body["items"] if i["marketer_id"] == str(b.id))
    assert item_b["attempt"] == 2 and item_b["status"] == "paid"


def test_retry_of_a_draft_batch_is_409_and_sends_nothing(db_session, pg_client, auth, use_provider):
    provider = use_provider(FakeProvider())
    batch, _, _ = _batch_of(db_session)

    resp = pg_client.post(f"{BASE}/batches/{batch.id}/retry", headers=auth)

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "batch_state_invalid"
    assert provider.pay_calls == []
    db_session.expire_all()
    assert db_session.get(PayoutBatch, batch.id).status == BatchStatus.DRAFT


# --- mark paid -------------------------------------------------------------------------------


def test_mark_paid_records_the_bank_reference_and_pays_the_rewards(
    db_session, pg_client, auth, use_provider
):
    use_provider(FakeProvider(pay_fn=_manual_result))
    batch, (a, b), (ra, rb) = _batch_of(db_session)
    _approve(pg_client, auth, batch.id)

    resp = _mark_paid(pg_client, auth, batch.id, [(a.id, "  BANK-REF-A  "), (b.id, "BANK-REF-B")])

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "paid"
    refs = {i["marketer_id"]: i["transfer_reference"] for i in body["items"]}
    assert refs == {str(a.id): "BANK-REF-A", str(b.id): "BANK-REF-B"}  # trimmed
    assert {i["status"] for i in body["items"]} == {"paid"}
    db_session.expire_all()
    assert db_session.get(ReferralReward, ra.id).status == RewardStatus.PAID
    assert db_session.get(ReferralReward, ra.id).transfer_reference == "BANK-REF-A"
    _no_leak(resp)


def test_mark_paid_refuses_a_processing_item_with_item_not_markable(
    db_session, pg_client, auth, use_provider
):
    def one_pending(marketer, amount, reference):
        status = "pending" if marketer.id == stuck_id else "manual"
        return TransferResult(status, reference, status)

    batch, (a, b), _ = _batch_of(db_session)
    stuck_id = b.id
    use_provider(FakeProvider(pay_fn=one_pending))
    _approve(pg_client, auth, batch.id)

    resp = _mark_paid(pg_client, auth, batch.id, [(a.id, "BANK-REF-A"), (b.id, "BANK-REF-B")])

    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "item_not_markable"
    assert detail["item_status"] == "processing"
    assert detail["marketer_id"] == str(b.id)
    # all or nothing: the awaiting_manual item was not touched either
    assert _item(db_session, batch, a).status == PS.AWAITING_MANUAL
    assert _item(db_session, batch, b).status == PS.PROCESSING


def test_mark_paid_with_an_unknown_marketer_is_404_and_changes_nothing(
    db_session, pg_client, auth, use_provider
):
    use_provider(FakeProvider(pay_fn=_manual_result))
    batch, (a, _), _ = _batch_of(db_session)
    _approve(pg_client, auth, batch.id)

    resp = _mark_paid(pg_client, auth, batch.id, [(a.id, "BANK-REF-A"), (uuid4(), "BANK-REF-X")])

    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"]["code"] == "payout_item_not_found"
    assert _item(db_session, batch, a).status == PS.AWAITING_MANUAL


def test_mark_paid_body_validation(db_session, pg_client, auth, use_provider):
    use_provider(FakeProvider(pay_fn=_manual_result))
    batch, (a, b), _ = _batch_of(db_session)
    _approve(pg_client, auth, batch.id)
    url = f"{BASE}/batches/{batch.id}/mark-paid"
    mid = str(a.id)

    bad_bodies = [
        {},
        {"items": []},
        {"items": [{"marketer_id": mid}]},
        {"items": [{"marketer_id": mid, "bank_reference": "", "amount_kobo": 500000}]},
        {"items": [{"marketer_id": mid, "bank_reference": "   ", "amount_kobo": 500000}]},
        {"items": [{"marketer_id": mid, "bank_reference": "x" * 101, "amount_kobo": 500000}]},
        {"items": [{"marketer_id": "not-a-uuid", "bank_reference": "R1", "amount_kobo": 500000}]},
        {
            "items": [
                {"marketer_id": mid, "bank_reference": "R1", "amount_kobo": 500000, "extra": 1}
            ]
        },
        {
            "items": [{"marketer_id": mid, "bank_reference": "R1", "amount_kobo": 500000}] * 2
        },  # same marketer twice
        {
            "items": [
                {"marketer_id": mid, "bank_reference": "SAME", "amount_kobo": 500000},
                {"marketer_id": str(b.id), "bank_reference": "SAME", "amount_kobo": 500000},
            ]
        },
        # the amount is required, a positive integer number of kobo
        {"items": [{"marketer_id": mid, "bank_reference": "R1"}]},
        {"items": [{"marketer_id": mid, "bank_reference": "R1", "amount_kobo": 0}]},
        {"items": [{"marketer_id": mid, "bank_reference": "R1", "amount_kobo": -5}]},
        {"items": [{"marketer_id": mid, "bank_reference": "R1", "amount_kobo": "500000"}]},
        {"items": [{"marketer_id": mid, "bank_reference": "R1", "amount_kobo": 5000.5}]},
        {"items": [{"marketer_id": mid, "bank_reference": "R1", "amount_kobo": True}]},
    ]
    for body in bad_bodies:
        resp = pg_client.post(url, json=body, headers=auth)
        assert resp.status_code == 422, (body, resp.text)
    assert _item(db_session, batch, a).status == PS.AWAITING_MANUAL

    ok = pg_client.post(
        url,
        json={"items": [{"marketer_id": mid, "bank_reference": "x" * 100, "amount_kobo": 500000}]},
        headers=auth,
    )
    assert ok.status_code == 200, ok.text  # 100 chars is allowed


def test_mark_paid_of_a_failed_item_needs_the_transfer_verified_dead(
    db_session, pg_client, auth, use_provider
):
    batch, (a, b), _ = _batch_of(db_session)
    provider = use_provider(
        FakeProvider(pay_fn=_failing_for(b.id), verify_fn=_verify_as("pending"))
    )
    _approve(pg_client, auth, batch.id)
    assert _item(db_session, batch, b).status == PS.FAILED

    # still pending at the provider: refused with 409, and no internal text in the body
    refused = _mark_paid(pg_client, auth, batch.id, [(b.id, "BANK-REF-B")])
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["code"] == "transfer_unverified"
    assert _item(db_session, batch, b).status == PS.FAILED

    # the provider cannot answer at all: also 409, and its error text is not echoed
    def boom(reference):
        raise PayoutProviderError("upstream-secret-detail-9f3")

    provider._verify_fn = boom
    unverifiable = _mark_paid(pg_client, auth, batch.id, [(b.id, "BANK-REF-B")])
    assert unverifiable.status_code == 409, unverifiable.text
    assert unverifiable.json()["detail"]["code"] == "transfer_unverified"
    assert "upstream-secret-detail-9f3" not in unverifiable.text
    assert "PayoutProviderError" not in unverifiable.text

    # verified failed: allowed
    provider._verify_fn = lambda ref: TransferVerification("failed", ref, "failed")
    ok = _mark_paid(pg_client, auth, batch.id, [(b.id, "BANK-REF-B")])
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "paid"


def test_mark_paid_on_a_draft_batch_is_409_batch_state_invalid(
    db_session, pg_client, auth, use_provider
):
    use_provider(FakeProvider())
    batch, (a, _), _ = _batch_of(db_session)

    resp = _mark_paid(pg_client, auth, batch.id, [(a.id, "BANK-REF-A")])

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "batch_state_invalid"


def test_reusing_a_bank_reference_is_409(db_session, pg_client, auth, use_provider):
    use_provider(FakeProvider(pay_fn=_manual_result))
    batch, (a, b), _ = _batch_of(db_session)
    _approve(pg_client, auth, batch.id)
    assert _mark_paid(pg_client, auth, batch.id, [(a.id, "BANK-REF-A")]).status_code == 200

    resp = _mark_paid(pg_client, auth, batch.id, [(b.id, "BANK-REF-A")])

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "bank_reference_in_use"
    assert _item(db_session, batch, b).status == PS.AWAITING_MANUAL


# --- no bank detail leak ---------------------------------------------------------------------


def test_no_response_contains_bank_details_or_the_recipient_code(
    db_session, pg_client, auth, use_provider
):
    marketer = _marketer(db_session)
    marketer.bank_code = "058"
    marketer.account_number = LEAK_ACCOUNT
    marketer.account_name = LEAK_NAME
    marketer.paystack_recipient_code = LEAK_RECIPIENT
    _reward(db_session, marketer)
    batch = prepare_batch(db_session, NOW)
    provider = use_provider(FakeProvider(pay_fn=_manual_result, verify_fn=_verify_as("failed")))

    responses = [
        pg_client.get(f"{BASE}/batches", headers=auth),
        pg_client.get(f"{BASE}/batches/{batch.id}", headers=auth),
        pg_client.post(f"{BASE}/batches/{batch.id}/retry", headers=auth),  # 409 on a draft
        _approve(pg_client, auth, batch.id),
        _approve(pg_client, auth, batch.id),
        pg_client.post(f"{BASE}/batches/{batch.id}/retry", headers=auth),
        _mark_paid(pg_client, auth, batch.id, [(uuid4(), "R-X")]),  # 404
        _mark_paid(pg_client, auth, batch.id, [(marketer.id, "BANK-REF-LEAK")]),
        _mark_paid(pg_client, auth, batch.id, [(marketer.id, "BANK-REF-LEAK-2")]),  # 409 paid
        pg_client.get(f"{BASE}/batches/{uuid4()}", headers=auth),
    ]

    codes = [r.status_code for r in responses]
    assert codes == [200, 200, 409, 200, 200, 200, 404, 200, 409, 404], codes
    for resp in responses:
        _no_leak(resp)
    detail = responses[1].json()["items"][0]
    assert detail["bank_last4"] == LEAK_ACCOUNT[-4:]
    assert provider.pay_calls  # the fake really did receive the marketer


# --- audit -----------------------------------------------------------------------------------


def _audit_rows(db, batch_id):
    db.expire_all()
    return list(
        db.scalars(
            select(AuditLog).where(
                AuditLog.resource_type == "payout_batch", AuditLog.resource_id == batch_id
            )
        )
    )


def test_each_write_is_audited_without_the_reference_or_bank_details(
    db_session, pg_client, auth, use_provider, super_admin_user
):
    use_provider(FakeProvider(pay_fn=_manual_result, verify_fn=_verify_as("failed")))
    batch, (a, b), _ = _batch_of(db_session)

    _approve(pg_client, auth, batch.id)
    _approve(pg_client, auth, batch.id)  # no-op: must not add a second approve entry
    pg_client.post(f"{BASE}/batches/{batch.id}/retry", headers=auth)
    _mark_paid(
        pg_client, auth, batch.id, [(a.id, "BANK-REF-SECRET-1"), (b.id, "BANK-REF-SECRET-2")]
    )

    rows = _audit_rows(db_session, batch.id)
    actions = sorted(r.action for r in rows)
    assert actions == [
        "approve_payout_batch",
        "mark_payout_batch_paid",
        "retry_payout_batch",
    ]
    for row in rows:
        blob = json.dumps(row.changes, default=str)
        assert "BANK-REF-SECRET" not in blob
        assert "0000000000" not in blob
        assert row.changes["performed_by_id"] == str(super_admin_user.id)
        assert row.changes["performed_by_type"] == "platform_admin"
        assert str(batch.id) in blob
    mark = next(r for r in rows if r.action == "mark_payout_batch_paid")
    assert mark.changes["metadata"]["items"] == 2


def test_audit_failure_does_not_fail_the_request_and_logs_class_and_ids_only(
    db_session, pg_client, auth, use_provider, monkeypatch
):
    use_provider(FakeProvider())
    batch, _, _ = _batch_of(db_session)
    batch_id = batch.id  # the failing audit path rolls the shared test session back

    def explode(self, *args, **kwargs):
        raise RuntimeError("audit-store-down-with-secret-text")

    monkeypatch.setattr(EnhancedAuditService, "log_action", explode)
    messages: list[str] = []
    sink_id = logger.add(lambda m: messages.append(str(m)), level="DEBUG")
    try:
        resp = _approve(pg_client, auth, batch.id)
    finally:
        logger.remove(sink_id)

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "paid"
    logged = "\n".join(messages)
    assert "RuntimeError" in logged and str(batch_id) in logged
    assert "audit-store-down-with-secret-text" not in logged


def test_a_rejected_write_writes_no_audit_row(db_session, pg_client, auth, use_provider):
    use_provider(FakeProvider())
    batch, (a, _), _ = _batch_of(db_session)

    assert pg_client.post(f"{BASE}/batches/{batch.id}/retry", headers=auth).status_code == 409
    assert _mark_paid(pg_client, auth, batch.id, [(a.id, "R1")]).status_code == 409

    assert _audit_rows(db_session, batch.id) == []


# --- suspended marketer & skipped items show up -----------------------------------------------


def test_detail_shows_a_skipped_item_and_the_held_rewards_it_left_out(
    db_session, pg_client, auth, use_provider
):
    use_provider(FakeProvider())
    batch, (a, b), _ = _batch_of(db_session)
    b_row = db_session.get(Marketer, b.id)
    b_row.status = MarketerStatus.SUSPENDED
    db_session.flush()

    body = _approve(pg_client, auth, batch.id).json()

    by_marketer = {i["marketer_id"]: i for i in body["items"]}
    assert by_marketer[str(b.id)]["status"] == "skipped"
    assert by_marketer[str(b.id)]["failure_reason"] == "marketer_suspended"
    assert by_marketer[str(b.id)]["held_rewards_excluded"] == 1
    assert by_marketer[str(a.id)]["status"] == "paid"
    assert body["total_kobo"] == 500_000
    assert body["item_counts"]["skipped"] == 1 and body["item_counts"]["paid"] == 1


def test_r49_mark_paid_accepts_the_claimed_or_the_current_amount_after_a_reward_was_held(
    db_session, pg_client, auth, use_provider
):
    use_provider(FakeProvider(pay_fn=_manual_result))
    m = _marketer(db_session)
    r1 = _reward(db_session, m)
    r2 = _reward(db_session, m, amount=300_000)
    batch = prepare_batch(db_session, NOW)
    assert _approve(pg_client, auth, batch.id).status_code == 200
    db_session.expire_all()
    db_session.get(ReferralReward, r2.id).status = RewardStatus.HELD  # a dispute, link kept
    db_session.flush()

    row = pg_client.get(f"{BASE}/batches/{batch.id}", headers=auth).json()["items"][0]
    assert row["amount_kobo"] == 800_000 and row["current_amount_kobo"] == 500_000

    wrong = _mark_paid(pg_client, auth, batch.id, [(m.id, "BANK-REF-X", 650_000)])
    assert wrong.status_code == 409
    detail = wrong.json()["detail"]
    assert detail["code"] == "amount_mismatch"
    assert detail["current_amount_kobo"] == 500_000 and detail["claimed_amount_kobo"] == 800_000

    ok = _mark_paid(pg_client, auth, batch.id, [(m.id, "BANK-REF-X", 800_000)])
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "paid"
    db_session.expire_all()
    assert db_session.get(ReferralReward, r1.id).status == RewardStatus.PAID
    assert db_session.get(ReferralReward, r2.id).status == RewardStatus.PAID


def test_mark_paid_with_an_amount_that_differs_is_409_amount_mismatch_and_changes_nothing(
    db_session, pg_client, auth, use_provider
):
    use_provider(FakeProvider(pay_fn=_manual_result))
    batch, (a, _), (ra, _) = _batch_of(db_session)
    _approve(pg_client, auth, batch.id)

    resp = _mark_paid(pg_client, auth, batch.id, [(a.id, "BANK-REF-A", 123_456)])

    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "amount_mismatch"
    assert detail["marketer_id"] == str(a.id) and detail["current_amount_kobo"] == 500_000
    assert detail["claimed_amount_kobo"] == 500_000  # awaiting_manual: the claimed amount too
    assert "123456" not in resp.text.replace("123_456", "")  # the submitted figure is not echoed
    assert _item(db_session, batch, a).status == PS.AWAITING_MANUAL
    assert _mark_paid(pg_client, auth, batch.id, [(a.id, "BANK-REF-A", 500_000)]).status_code == 200


# --- prepare a batch by hand (M6) --------------------------------------------------------------


def _unbatch(db):
    """Other tests' leftovers must not be payable in the batch this test prepares."""
    db.query(ReferralReward).filter(ReferralReward.status == RewardStatus.PAYABLE).update(
        {"status": RewardStatus.HELD}
    )
    db.flush()


def test_prepare_endpoint_creates_a_draft_batch_audits_ids_and_counts_and_is_idempotent(
    db_session, pg_client, auth, use_provider, super_admin_user
):
    provider = use_provider(FakeProvider())
    _unbatch(db_session)
    a, b = _marketer(db_session), _marketer(db_session)
    _reward(db_session, a)
    _reward(db_session, b, amount=300_000)

    first = pg_client.post(f"{BASE}/batches/prepare", headers=auth)

    assert first.status_code == 200, first.text
    body = first.json()
    detail = body["batch"]
    assert detail["status"] == "draft" and detail["total_kobo"] == 800_000
    assert detail["item_counts"]["pending"] == 2 and len(detail["items"]) == 2
    _no_leak(first)
    batch_id = UUID(detail["id"])
    rows = _audit_rows(db_session, batch_id)
    assert [r.action for r in rows] == ["prepare_payout_batch"]
    blob = json.dumps(rows[0].changes, default=str)
    assert rows[0].changes["metadata"]["total_kobo"] == 800_000
    assert rows[0].changes["performed_by_id"] == str(super_admin_user.id)
    assert "0000000000" not in blob and "bank" not in blob
    assert provider.pay_calls == []  # preparing never sends anything

    second = pg_client.post(f"{BASE}/batches/prepare", headers=auth)  # idempotent
    assert second.status_code == 200
    assert second.json() == {"batch": None}


def test_prepare_endpoint_with_nothing_payable_is_200_with_a_null_batch_and_no_audit(
    db_session, pg_client, auth
):
    _unbatch(db_session)
    before = db_session.scalar(
        select(func.count()).select_from(AuditLog).where(AuditLog.action == "prepare_payout_batch")
    )

    resp = pg_client.post(f"{BASE}/batches/prepare", headers=auth)

    assert resp.status_code == 200 and resp.json() == {"batch": None}
    after = db_session.scalar(
        select(func.count()).select_from(AuditLog).where(AuditLog.action == "prepare_payout_batch")
    )
    assert after == before


def test_prepare_route_is_not_shadowed_by_the_batch_id_routes(pg_client, auth):
    paths = [r.path for r in pg_client.app.routes if hasattr(r, "path")]
    prepare = f"{BASE}/batches/prepare"
    by_id = f"{BASE}/batches/{{batch_id:uuid}}"
    assert prepare in paths
    assert paths.index(prepare) < paths.index(by_id)
    assert pg_client.get(f"{BASE}/batches/not-a-uuid", headers=auth).status_code == 404
