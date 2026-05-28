"""Test that ActionSecret.secret_value is encrypted at rest."""

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
    """Separate in-memory DB for encryption tests to avoid conftest conflicts."""
    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)  # noqa: N806
    session = Session()
    yield session
    session.close()


class TestActionSecretEncryption:
    def test_secret_value_encrypted_in_database(self, encryption_db):
        import uuid
        from app.db.models.automation import ActionSecret

        secret = ActionSecret(
            id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            provider="stripe",
            key_name="api_key",
            secret_value="sk_live_abc123",
            status="active",
        )
        encryption_db.add(secret)
        encryption_db.commit()

        # Read raw value from DB — should NOT be plaintext
        row = encryption_db.execute(
            text("SELECT secret_value FROM action_secrets LIMIT 1")
        ).fetchone()
        raw_value = row[0]
        assert raw_value != "sk_live_abc123", "secret_value stored as plaintext!"

        # Read through ORM — should be decrypted
        encryption_db.expire_all()
        loaded = encryption_db.query(ActionSecret).first()
        assert loaded.secret_value == "sk_live_abc123"

    def test_secret_value_roundtrip(self, encryption_db):
        import uuid
        from app.db.models.automation import ActionSecret

        original = "bearer-token-xyz-789"
        secret = ActionSecret(
            id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            provider="custom_api",
            key_name="auth_token",
            secret_value=original,
            status="active",
        )
        encryption_db.add(secret)
        encryption_db.commit()

        encryption_db.expire_all()
        loaded = encryption_db.query(ActionSecret).first()
        assert loaded.secret_value == original
