"""Test PaymentTransaction encryption for authorization_code and paystack_data."""

import json
import uuid
from datetime import datetime, UTC

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.db.base import Base


@pytest.fixture(autouse=True)
def _set_encryption_env(monkeypatch):
    test_key = Fernet.generate_key().decode()
    monkeypatch.setenv("ENCRYPTION_KEY", test_key)
    monkeypatch.setenv("ENCRYPTION_MIGRATION_MODE", "true")


@pytest.fixture
def encryption_db():
    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)  # noqa: N806
    session = Session()
    yield session
    session.close()


SAMPLE_PAYSTACK_DATA = {
    "reference": "txn_abc123",
    "amount": 500000,
    "currency": "NGN",
    "status": "success",
    "plan_code": "PLN_test",
    "paid_at": "2026-04-01T12:00:00Z",
    "channel": "card",
    "transaction_date": "2026-04-01T12:00:00Z",
    "authorization": {"authorization_code": "AUTH_secret", "card_type": "visa"},
    "customer": {"customer_code": "CUS_secret", "email": "user@test.com"},
}

ALLOWED_KEYS = {
    "reference",
    "amount",
    "currency",
    "status",
    "plan_code",
    "paid_at",
    "channel",
    "transaction_date",
}


class TestPaymentEncryption:
    def test_authorization_code_encrypted_in_db(self, encryption_db):
        from app.db.models.payment import PaymentTransaction

        txn = PaymentTransaction(
            id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            reference="ref_001",
            amount=100000,
            currency="NGN",
            status="success",
            tier="lite",
            billing_cycle="monthly",
            authorization_code="AUTH_secret_code",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        encryption_db.add(txn)
        encryption_db.commit()

        row = encryption_db.execute(
            text("SELECT authorization_code FROM payment_transactions LIMIT 1")
        ).fetchone()
        assert row[0] != "AUTH_secret_code", "authorization_code stored as plaintext!"

        encryption_db.expire_all()
        loaded = encryption_db.query(PaymentTransaction).first()
        assert loaded.authorization_code == "AUTH_secret_code"

    def test_paystack_data_encrypted_stripped_roundtrip(self, encryption_db):
        from app.db.models.payment import PaymentTransaction, strip_paystack_data

        stripped = strip_paystack_data(SAMPLE_PAYSTACK_DATA)
        assert set(stripped.keys()) <= ALLOWED_KEYS
        assert "authorization" not in stripped
        assert "customer" not in stripped

        txn = PaymentTransaction(
            id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            reference="ref_002",
            amount=500000,
            currency="NGN",
            status="success",
            tier="professional",
            billing_cycle="annual",
            paystack_data_encrypted=json.dumps(stripped),
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        encryption_db.add(txn)
        encryption_db.commit()

        # Raw DB value should be encrypted
        row = encryption_db.execute(
            text("SELECT paystack_data_encrypted FROM payment_transactions LIMIT 1")
        ).fetchone()
        raw = row[0]
        assert raw is not None
        assert "reference" not in raw  # encrypted, not readable JSON

        # ORM read should decrypt
        encryption_db.expire_all()
        loaded = encryption_db.query(PaymentTransaction).first()
        parsed = json.loads(loaded.paystack_data_encrypted)
        assert parsed["reference"] == "txn_abc123"
        assert parsed["amount"] == 500000
