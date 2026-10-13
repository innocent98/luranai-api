"""Two parallel manual requests for one ticket -> exactly one request (spec §4.1 lock)."""

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
from app.db.models.review import RatingRequest, RatingSource
from app.db.models.tenant import Tenant, TenantStatus
from app.db.models.user import User, UserRole
from app.services.rating_request_service import RatingRequestError, RatingRequestService
from app.utils.datetime import utcnow_naive

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL", "").startswith("postgresql"),
    reason="row-lock race needs Postgres (CI test job / sr-ci-pg)",
)


def test_parallel_manual_requests_create_exactly_one(pg_engine):
    Session = sessionmaker(bind=pg_engine)
    seed = Session()
    tenant = Tenant(
        name=f"n4race-{uuid.uuid4().hex[:8]}",
        display_name="Race Co",
        status=TenantStatus.ACTIVE,
        api_key_hash=uuid.uuid4().hex,
        subscription_status="active",
        subscription_tier="growth",
    )
    seed.add(tenant)
    seed.flush()
    admin = User(
        email=f"{uuid.uuid4().hex[:10]}@example.com",
        hashed_password="x",
        full_name="Race Admin",
        role=UserRole.TENANT_ADMIN,
        tenant_id=tenant.id,
        is_active=True,
    )
    agent = User(
        email=f"{uuid.uuid4().hex[:10]}@example.com",
        hashed_password="x",
        full_name="Race Agent",
        role=UserRole.SUPPORT_AGENT,
        tenant_id=tenant.id,
        is_active=True,
    )
    seed.add_all([admin, agent])
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
        ticket_number=f"TKT-RACE-{uuid.uuid4().hex[:8]}",
        summary="race",
        priority=TicketPriority.MEDIUM,
        status=TicketStatus.IN_PROGRESS,
        assigned_to=agent.id,
    )
    seed.add(ticket)
    seed.commit()
    ids = (tenant.id, admin.id, agent.id, session.id, ticket.id)
    seed.close()

    barrier = threading.Barrier(2)
    results: list[str] = []

    def worker():
        s = Session()
        try:
            actor = s.get(User, ids[1])
            barrier.wait(timeout=10)
            RatingRequestService(s).create_request(
                ids[4], source=RatingSource.MANUAL_REQUEST, requested_by=actor
            )
            results.append("created")
        except RatingRequestError as exc:
            results.append(exc.code)
        finally:
            s.close()

    try:
        threads = [threading.Thread(target=worker) for _ in range(2)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=30)
        assert sorted(results) == ["created", "rating_request_cooldown"]
        check = Session()
        try:
            n = check.execute(
                select(func.count(RatingRequest.id)).where(RatingRequest.session_id == ids[3])
            ).scalar_one()
            assert n == 1
        finally:
            check.close()
    finally:
        cleanup = Session()
        cleanup.execute(delete(RatingRequest).where(RatingRequest.session_id == ids[3]))
        cleanup.execute(delete(Message).where(Message.session_id == ids[3]))
        cleanup.execute(delete(Ticket).where(Ticket.id == ids[4]))
        cleanup.execute(delete(ConversationSession).where(ConversationSession.id == ids[3]))
        cleanup.execute(delete(User).where(User.id.in_([ids[1], ids[2]])))
        cleanup.execute(delete(Tenant).where(Tenant.id == ids[0]))
        cleanup.commit()
        cleanup.close()
