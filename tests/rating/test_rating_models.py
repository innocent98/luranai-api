"""N4-D1/D2/D7: per-party uniqueness, source CHECK, encrypted comment, token uniqueness."""

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core.encryption import decrypt_field
from app.db.models.review import (
    RATING_REQUEST_SOURCE_CHECK,
    SESSION_RATING_SOURCE_CHECK,
    RatingRequest,
    RatingSource,
    SessionRating,
)
from app.utils.datetime import utcnow_aware
from tests.rating.conftest import make_rating, make_ticket


def test_check_constants_match_spec_values():
    assert SESSION_RATING_SOURCE_CHECK == (
        "source IN ('idle_prompt', 'resolution_prompt', 'manual_request', 'auto_resolve')"
    )
    assert RATING_REQUEST_SOURCE_CHECK == "source IN ('manual_request', 'auto_resolve')"
    assert RatingSource.ALL == (
        "idle_prompt",
        "resolution_prompt",
        "manual_request",
        "auto_resolve",
    )


def test_source_defaults_to_idle_prompt(db, tenant):
    t = make_ticket(db, tenant)
    rating = SessionRating(
        tenant_id=tenant.id, session_id=t.session_id, score=4, user_identifier="v"
    )
    db.add(rating)
    db.commit()
    assert rating.source == "idle_prompt"
    assert rating.rated_agent_id is None


def test_ai_and_each_agent_rate_once_per_session(db, tenant, agent, other_agent):
    t = make_ticket(db, tenant)
    make_rating(db, session_id=t.session_id, tenant_id=tenant.id)
    make_rating(db, session_id=t.session_id, tenant_id=tenant.id, rated_agent_id=agent.id)
    make_rating(db, session_id=t.session_id, tenant_id=tenant.id, rated_agent_id=other_agent.id)
    db.add(
        SessionRating(
            tenant_id=tenant.id,
            session_id=t.session_id,
            score=1,
            user_identifier="v",
            rated_agent_id=agent.id,
        )
    )
    with pytest.raises(IntegrityError):
        db.flush()


def test_second_ai_rating_rejected(db, tenant):
    t = make_ticket(db, tenant)
    make_rating(db, session_id=t.session_id, tenant_id=tenant.id)
    db.add(
        SessionRating(tenant_id=tenant.id, session_id=t.session_id, score=2, user_identifier="v")
    )
    with pytest.raises(IntegrityError):
        db.flush()


def test_unknown_source_rejected_by_check(db, tenant):
    t = make_ticket(db, tenant)
    db.add(
        SessionRating(
            tenant_id=tenant.id,
            session_id=t.session_id,
            score=2,
            user_identifier="v",
            source="widget_magic",
        )
    )
    with pytest.raises(IntegrityError):
        db.flush()


def test_comment_is_encrypted_at_rest(db, tenant):
    t = make_ticket(db, tenant)
    r = make_rating(db, session_id=t.session_id, tenant_id=tenant.id, comment="great help")
    raw = db.execute(
        text("SELECT comment FROM session_ratings WHERE id = :id"), {"id": r.id.hex}
    ).scalar_one()
    assert raw != "great help"
    assert decrypt_field(raw) == "great help"
    db.expire_all()
    assert db.get(SessionRating, r.id).comment == "great help"


def test_rating_request_token_hash_is_unique(db, tenant, agent):
    t = make_ticket(db, tenant, assigned_to=agent.id)
    h = uuid4().hex + uuid4().hex
    for _ in range(2):
        db.add(
            RatingRequest(
                tenant_id=tenant.id,
                session_id=t.session_id,
                ticket_id=t.id,
                agent_id=agent.id,
                source=RatingSource.MANUAL_REQUEST,
                token_hash=h,
                created_at=utcnow_aware(),
                expires_at=utcnow_aware() + timedelta(days=7),
            )
        )
    with pytest.raises(IntegrityError):
        db.flush()
