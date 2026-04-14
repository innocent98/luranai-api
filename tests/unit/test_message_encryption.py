"""Test that Message.content and Message.meta are encrypted at rest."""

import json
import uuid
from datetime import datetime, timezone

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
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


class TestMessageEncryption:
    def test_content_encrypted_in_db(self, encryption_db):
        from app.db.models.conversation import Message, MessageRole

        msg = Message(
            id=uuid.uuid4(),
            session_id=uuid.uuid4(),
            role=MessageRole.USER,
            content="My credit card number is 4111-1111-1111-1111",
            created_at=datetime.now(timezone.utc),
        )
        encryption_db.add(msg)
        encryption_db.commit()

        row = encryption_db.execute(text("SELECT content FROM messages LIMIT 1")).fetchone()
        assert "4111" not in row[0], "content stored as plaintext!"

        encryption_db.expire_all()
        loaded = encryption_db.query(Message).first()
        assert loaded.content == "My credit card number is 4111-1111-1111-1111"

    def test_meta_encrypted_in_db(self, encryption_db):
        from app.db.models.conversation import Message, MessageRole

        meta = {"user_agent": "Mozilla/5.0", "ip_hash": "abc123", "location": "Lagos"}
        msg = Message(
            id=uuid.uuid4(),
            session_id=uuid.uuid4(),
            role=MessageRole.ASSISTANT,
            content="Hello",
            meta=meta,
            created_at=datetime.now(timezone.utc),
        )
        encryption_db.add(msg)
        encryption_db.commit()

        row = encryption_db.execute(text("SELECT meta FROM messages LIMIT 1")).fetchone()
        raw = row[0]
        assert "Mozilla" not in str(raw), "meta stored as plaintext!"

        encryption_db.expire_all()
        loaded = encryption_db.query(Message).first()
        assert loaded.meta == meta
