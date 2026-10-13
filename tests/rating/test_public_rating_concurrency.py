"""Concurrent double-submit of one rating token -> exactly one rating (spec §4.3.3)."""

import os
import threading
import uuid

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.orm import sessionmaker

from app.db.models.conversation import (
    ConversationSession,
    Message,
    SessionStatus,
    Ticket,
    TicketPriority,
    TicketStatus,
)
from app.db.models.review import RatingRequest, RatingSource, SessionRating
from app.db.models.tenant import Tenant, TenantStatus
from app.db.models.user import User, UserRole
from app.schemas.review import SessionRatingCreate
from app.services.rating_request_service import RatingRequestService
from app.services.review_service import RatingSubmitError, ReviewService
from app.utils.datetime import utcnow_naive

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL", "").startswith("postgresql"),
    reason="row-lock race needs Postgres (CI test job / sr-ci-pg)",
)


def test_parallel_token_submits_create_exactly_one_rating(pg_engine):
    Session = sessionmaker(bind=pg_engine)
    seed = Session()
    tenant = Tenant(
        name=f"n4pub-{uuid.uuid4().hex[:8]}",
        display_name="Race Co",
        status=TenantStatus.ACTIVE,
        api_key_hash=uuid.uuid4().hex,
        subscription_status="active",
        subscription_tier="growth",
    )
    seed.add(tenant)
    seed.flush()
    agent = User(
        email=f"{uuid.uuid4().hex[:10]}@example.com",
        hashed_password="x",
        full_name="Race Agent",
        role=UserRole.SUPPORT_AGENT,
        tenant_id=tenant.id,
        is_active=True,
    )
    seed.add(agent)
    seed.flush()
    session = ConversationSession(
        tenant_id=tenant.id,
        user_identifier="race-visitor",
        status=SessionStatus.ESCALATED,
        escalated_at=utcnow_naive(),
    )
    seed.add(session)
    seed.flush()
    ticket = Ticket(
        tenant_id=tenant.id,
        session_id=session.id,
        ticket_number=f"TKT-PUB-{uuid.uuid4().hex[:8]}",
        summary="race",
        priority=TicketPriority.MEDIUM,
        status=TicketStatus.IN_PROGRESS,
        assigned_to=agent.id,
    )
    seed.add(ticket)
    seed.commit()
    res = RatingRequestService(seed).create_request(
        ticket.id, source=RatingSource.MANUAL_REQUEST, requested_by=agent
    )
    token = res.token
    ids = (tenant.id, agent.id, session.id, ticket.id)
    seed.close()

    barrier = threading.Barrier(2)
    results: list[str] = []

    def worker():
        s = Session()
        try:
            payload = SessionRatingCreate(
                session_id=ids[2],
                user_identifier="race-visitor",
                score=5,
                rating_request_token=token,
            )
            barrier.wait(timeout=10)
            ReviewService(s).create_session_rating(payload)
            results.append("created")
        except RatingSubmitError as exc:
            results.append(exc.code)
        finally:
            s.close()

    try:
        threads = [threading.Thread(target=worker) for _ in range(2)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=30)
        assert sorted(results) == ["already_rated", "created"]
        check = Session()
        try:
            n = check.execute(
                select(func.count(SessionRating.id)).where(SessionRating.session_id == ids[2])
            ).scalar_one()
            assert n == 1
        finally:
            check.close()
    finally:
        cleanup = Session()
        cleanup.execute(delete(SessionRating).where(SessionRating.session_id == ids[2]))
        cleanup.execute(delete(RatingRequest).where(RatingRequest.session_id == ids[2]))
        cleanup.execute(delete(Message).where(Message.session_id == ids[2]))
        cleanup.execute(delete(Ticket).where(Ticket.id == ids[3]))
        cleanup.execute(delete(ConversationSession).where(ConversationSession.id == ids[2]))
        cleanup.execute(delete(User).where(User.id == ids[1]))
        cleanup.execute(delete(Tenant).where(Tenant.id == ids[0]))
        cleanup.commit()
        cleanup.close()
