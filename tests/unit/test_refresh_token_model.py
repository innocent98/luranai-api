"""Test RefreshToken model creation."""

import uuid
from datetime import datetime, timedelta, UTC

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.models.refresh_token import RefreshToken  # noqa: F401 — register model with Base


@pytest.fixture
def model_db():
    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)  # noqa: N806
    session = Session()
    yield session
    session.close()


class TestRefreshTokenModel:
    def test_create_refresh_token(self, model_db):
        from app.db.models.refresh_token import RefreshToken

        token = RefreshToken(
            id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            user_type="tenant_user",
            token_hash="a" * 64,
            expires_at=datetime.now(UTC) + timedelta(days=7),
            created_at=datetime.now(UTC),
        )
        model_db.add(token)
        model_db.commit()

        loaded = model_db.query(RefreshToken).first()
        assert loaded.token_hash == "a" * 64
        assert loaded.user_type == "tenant_user"
        assert loaded.revoked_at is None

    def test_revoke_refresh_token(self, model_db):

        token = RefreshToken(
            id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            user_type="customer",
            token_hash="b" * 64,
            expires_at=datetime.now(UTC) + timedelta(days=7),
            created_at=datetime.now(UTC),
        )
        model_db.add(token)
        model_db.commit()

        token.revoked_at = datetime.now(UTC)
        model_db.commit()

        loaded = model_db.query(RefreshToken).first()
        assert loaded.revoked_at is not None
