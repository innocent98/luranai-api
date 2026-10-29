"""charge.success webhook handling must be idempotent under replay."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import event

from app.db.models.payment import PaymentTransaction
from app.db.models.subscription import SubscriptionHistory
from app.db.models.tenant import Tenant, TenantCapabilities
from app.services.webhook_handler_service import WebhookHandlerService


@pytest.fixture
def pending_payment(db_session):
    """A trial-expired tenant with a pending professional/monthly transaction."""
    tenant = Tenant(
        name=f"t-{uuid4().hex[:8]}",
        display_name="Idempotency Test",
        api_key_hash="dummy",
        subscription_tier="trial",
        subscription_status="trial_expired",
        trial_ends_at=datetime.now(UTC) - timedelta(days=3),
    )
    db_session.add(tenant)
    db_session.flush()
    db_session.add(TenantCapabilities(tenant_id=tenant.id))

    reference = f"ref_{uuid4().hex[:12]}"
    transaction = PaymentTransaction(
        tenant_id=tenant.id,
        reference=reference,
        amount=1000000,
        tier="professional",
        billing_cycle="monthly",
        status="pending",
    )
    db_session.add(transaction)
    db_session.commit()
    return tenant, transaction, reference


def _payload(reference: str) -> dict:
    return {
        "reference": reference,
        "channel": "card",
        "customer": {"email": "buyer@example.com", "customer_code": "CUS_placeholder"},
        "authorization": {"authorization_code": "AUTH_placeholder"},
    }


def _upgrade_rows(db_session, tenant_id):
    return (
        db_session.query(SubscriptionHistory)
        .filter(
            SubscriptionHistory.tenant_id == tenant_id,
            SubscriptionHistory.action == "upgrade",
        )
        .all()
    )


def test_first_delivery_unchanged(db_session, pending_payment):
    tenant, transaction, reference = pending_payment

    result = WebhookHandlerService(db_session).handle_event("charge.success", _payload(reference))

    assert result["status"] == "success"
    assert result["transaction_id"] == str(transaction.id)
    db_session.refresh(transaction)
    db_session.refresh(tenant)
    assert transaction.status == "success"
    assert transaction.paid_at is not None
    assert transaction.channel == "card"
    assert tenant.subscription_tier == "professional"
    assert tenant.subscription_status == "active"
    rows = _upgrade_rows(db_session, tenant.id)
    assert len(rows) == 1
    assert rows[0].previous_tier == "trial"
    assert rows[0].new_tier == "professional"


def test_replay_does_not_rerun_upgrade(db_session, pending_payment, monkeypatch):
    tenant, transaction, reference = pending_payment
    handler = WebhookHandlerService(db_session)
    handler.handle_event("charge.success", _payload(reference))

    db_session.refresh(tenant)
    tier_after_first = tenant.subscription_tier
    starts_after_first = tenant.subscription_starts_at

    calls = []
    original = handler.subscription_service.upgrade_subscription
    monkeypatch.setattr(
        handler.subscription_service,
        "upgrade_subscription",
        lambda **kw: calls.append(kw) or original(**kw),
    )

    result = handler.handle_event("charge.success", _payload(reference))

    assert result == {"status": "already_processed"}
    assert calls == []
    db_session.refresh(tenant)
    assert tenant.subscription_tier == tier_after_first
    assert tenant.subscription_starts_at == starts_after_first


def test_replay_does_not_add_history_row(db_session, pending_payment):
    tenant, transaction, reference = pending_payment
    handler = WebhookHandlerService(db_session)
    handler.handle_event("charge.success", _payload(reference))
    db_session.refresh(transaction)
    paid_at_after_first = transaction.paid_at
    data_after_first = transaction.paystack_data

    replay = _payload(reference)
    replay["channel"] = "bank"
    result = handler.handle_event("charge.success", replay)

    assert result == {"status": "already_processed"}
    assert len(_upgrade_rows(db_session, tenant.id)) == 1
    db_session.refresh(transaction)
    assert transaction.paid_at == paid_at_after_first
    assert transaction.channel == "card"
    assert transaction.paystack_data == data_after_first


def test_transaction_lookup_takes_row_lock(db_session, pending_payment):
    """The lookup must be SELECT ... FOR UPDATE so parallel deliveries serialise."""
    _, _, reference = pending_payment
    statements = []

    def capture(conn, cursor, statement, *args):
        statements.append(statement)

    engine = db_session.get_bind().engine
    event.listen(engine, "before_cursor_execute", capture)
    try:
        WebhookHandlerService(db_session).handle_event("charge.success", _payload(reference))
    finally:
        event.remove(engine, "before_cursor_execute", capture)

    locking = [s for s in statements if "payment_transactions" in s and "FOR UPDATE" in s]
    assert locking, "payment_transactions lookup was not issued with FOR UPDATE"
