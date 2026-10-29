"""Paystack ``transfer.*`` webhook events through the real endpoint, signature included.

The payloads below are SHAPED FROM THE PUBLIC DOCS (event name + ``data.reference`` /
``data.amount`` / ``data.status``), not captured from Paystack. Signatures are built with the same
HMAC-SHA512 logic the endpoint verifies, using a placeholder secret set for the test only.
"""

import hashlib
import hmac
import json
from uuid import uuid4

import pytest

from app.core.config import settings
from app.db.models.referral import (
    BatchStatus,
    PayoutBatch,
    PayoutItemStatus,
    ReferralReward,
    RewardStatus,
)
from app.services.referral.payouts import approve_batch
from tests.referral.test_payouts import FakeProvider, _batch_of, _item, _pending_for

URL = "/api/v1/webhooks/paystack"
TEST_SECRET = "sk_test_placeholder_webhook_secret_not_real"


@pytest.fixture(autouse=True)
def _test_secret(monkeypatch):
    monkeypatch.setattr(settings, "PAYSTACK_SECRET_KEY", TEST_SECRET)


def _signed(event: str, data: dict, secret: str = TEST_SECRET):
    body = json.dumps({"event": event, "data": data}).encode()
    signature = hmac.new(secret.encode(), body, hashlib.sha512).hexdigest()
    return body, {"x-paystack-signature": signature, "content-type": "application/json"}


def _post(client, event, data, secret=TEST_SECRET):
    body, headers = _signed(event, data, secret)
    return client.post(URL, content=body, headers=headers)


def _processing_batch(db_session, admin):
    """A batch with one item left ``processing`` (Paystack accepted it, no final answer yet)."""
    batch, (marketer,), (reward,) = _batch_of(db_session, 1)
    approve_batch(db_session, batch.id, admin.id, FakeProvider(pay_fn=_pending_for(marketer.id)))
    item = _item(db_session, batch, marketer)
    assert item.status == PayoutItemStatus.PROCESSING
    return batch, marketer, reward, item.transfer_reference


def _reward(db_session, reward):
    db_session.expire_all()
    return db_session.get(ReferralReward, reward.id)


def test_transfer_webhook_signature_checked(pg_client, db_session, super_admin_user):
    batch, marketer, reward, ref = _processing_batch(db_session, super_admin_user)
    payload = {"reference": ref, "amount": 500_000, "status": "success"}

    # 1. wrong signature: 400 and nothing changes
    bad = _post(pg_client, "transfer.success", payload, secret="sk_test_some_other_secret")
    assert bad.status_code == 400
    assert bad.json() == {"error": "Invalid signature"}
    assert _item(db_session, batch, marketer).status == PayoutItemStatus.PROCESSING
    assert _reward(db_session, reward).status == RewardStatus.BATCHED

    # 1b. tampered body with the genuine signature of another body: also refused
    body, headers = _signed("transfer.failed", payload)
    tampered = pg_client.post(
        URL, content=body.replace(b"transfer.failed", b"transfer.success"), headers=headers
    )
    assert tampered.status_code == 400
    assert _item(db_session, batch, marketer).status == PayoutItemStatus.PROCESSING

    # 2. correct signature: the item and its rewards are paid
    ok = _post(pg_client, "transfer.success", payload)
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "success"
    assert ok.json()["result"]["status"] == "processed"
    assert _item(db_session, batch, marketer).status == PayoutItemStatus.PAID
    paid = _reward(db_session, reward)
    assert paid.status == RewardStatus.PAID and paid.transfer_reference == ref
    db_session.expire_all()
    assert db_session.get(PayoutBatch, batch.id).status == BatchStatus.PAID

    # 3. replaying the same signed event changes nothing
    paid_at = paid.paid_at
    replay = _post(pg_client, "transfer.success", payload)
    assert replay.status_code == 200
    assert replay.json()["result"]["status"] == "duplicate"
    assert _reward(db_session, reward).paid_at == paid_at
    assert _item(db_session, batch, marketer).status == PayoutItemStatus.PAID

    # 4. a signed reversal after success: item failed, reward batched again, batch not paid
    reversed_ = _post(pg_client, "transfer.reversed", {**payload, "status": "reversed"})
    assert reversed_.status_code == 200, reversed_.text
    assert reversed_.json()["result"]["status"] == "processed"
    item = _item(db_session, batch, marketer)
    assert item.status == PayoutItemStatus.FAILED and item.failure_reason == "reversed"
    back = _reward(db_session, reward)
    assert back.status == RewardStatus.BATCHED
    assert back.paid_at is None and back.transfer_reference is None
    db_session.expire_all()
    assert db_session.get(PayoutBatch, batch.id).status != BatchStatus.PAID


def test_transfer_failed_webhook_fails_a_processing_item(pg_client, db_session, super_admin_user):
    batch, marketer, reward, ref = _processing_batch(db_session, super_admin_user)

    resp = _post(
        pg_client,
        "transfer.failed",
        {"reference": ref, "amount": 500_000, "status": "failed", "message": "declined"},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["result"]["status"] == "processed"
    item = _item(db_session, batch, marketer)
    assert item.status == PayoutItemStatus.FAILED and item.failure_reason == "declined"
    assert _reward(db_session, reward).status == RewardStatus.BATCHED


def test_unknown_transfer_reference_is_ignored_with_200(pg_client, db_session):
    for event in ("transfer.success", "transfer.failed", "transfer.reversed"):
        resp = _post(pg_client, event, {"reference": f"ref-{uuid4()}-{uuid4()}", "amount": 100})
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "success"
        assert resp.json()["result"]["status"] == "ignored"
        assert resp.json()["result"]["reason"] == "unknown_reference"


def test_transfer_event_with_a_malformed_payload_is_ignored_not_a_500(pg_client, db_session):
    for data in ({}, {"reference": 5}, {"reference": ""}):
        resp = _post(pg_client, "transfer.success", data)
        assert resp.status_code == 200, resp.text
        assert resp.json()["result"]["status"] == "ignored"


def test_transfer_event_does_not_touch_unrelated_items(pg_client, db_session, super_admin_user):
    batch, marketer, reward, ref = _processing_batch(db_session, super_admin_user)
    other_batch, (other,), (other_reward,) = _batch_of(db_session, 1)

    resp = _post(pg_client, "transfer.success", {"reference": ref, "amount": 500_000})

    assert resp.status_code == 200
    assert _item(db_session, other_batch, other).status == PayoutItemStatus.PENDING
    assert _reward(db_session, other_reward).status == RewardStatus.BATCHED
