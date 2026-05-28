"""Test PII encryption and hash column lookups."""

import uuid
from datetime import datetime, UTC

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.core.encryption import hash_for_lookup


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


class TestCustomerUserPIIEncryption:
    def test_email_encrypted_in_db(self, encryption_db):
        from app.db.models.customer_user import CustomerUser

        user = CustomerUser(
            id=uuid.uuid4(),
            full_name="Alice Smith",
            email="alice@example.com",
            email_hash=hash_for_lookup("alice@example.com"),
            hashed_password="$2b$12$fakehash",
            is_active=True,
        )
        encryption_db.add(user)
        encryption_db.commit()

        row = encryption_db.execute(text("SELECT email FROM customer_users LIMIT 1")).fetchone()
        assert "alice" not in row[0], "email stored as plaintext!"

        encryption_db.expire_all()
        loaded = encryption_db.query(CustomerUser).first()
        assert loaded.email == "alice@example.com"

    def test_email_hash_lookup(self, encryption_db):
        from app.db.models.customer_user import CustomerUser

        user = CustomerUser(
            id=uuid.uuid4(),
            full_name="Bob Jones",
            email="bob@example.com",
            email_hash=hash_for_lookup("bob@example.com"),
            hashed_password="$2b$12$fakehash",
            is_active=True,
        )
        encryption_db.add(user)
        encryption_db.commit()

        found = (
            encryption_db.query(CustomerUser)
            .filter(CustomerUser.email_hash == hash_for_lookup("bob@example.com"))
            .first()
        )
        assert found is not None
        assert found.email == "bob@example.com"


class TestVisitorPIIEncryption:
    def test_visitor_email_encrypted(self, encryption_db):
        from app.db.models.visitor import Visitor, VisitorSource

        visitor = Visitor(
            id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            public_token="tok_abc123",
            email="visitor@test.com",
            email_hash=hash_for_lookup("visitor@test.com"),
            name="Test Visitor",
            first_seen_at=datetime.now(UTC),
            last_seen_at=datetime.now(UTC),
            created_from=VisitorSource.WIDGET,
        )
        encryption_db.add(visitor)
        encryption_db.commit()

        row = encryption_db.execute(text("SELECT email FROM visitors LIMIT 1")).fetchone()
        assert "visitor" not in str(row[0]), "visitor email stored as plaintext!"
