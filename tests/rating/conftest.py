"""Shared fixtures and factories for the N4 agent-rating tests (SQLite `db`)."""

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.core.security import create_access_token
from app.core.websocket import connection_manager
from app.db.models.conversation import (
    ConversationSession,
    SessionStatus,
    Ticket,
    TicketPriority,
    TicketStatus,
)
from app.db.models.review import RatingSource, SessionRating
from app.db.models.tenant import Tenant, TenantCapabilities, TenantStatus
from app.db.models.user import AvailabilityStatus, User, UserRole
from app.utils.datetime import utcnow_naive


def make_tenant(db) -> Tenant:
    tenant = Tenant(
        name=f"n4-{uuid.uuid4().hex[:8]}",
        display_name="Acme Test Co",
        status=TenantStatus.ACTIVE,
        api_key_hash=f"hash-{uuid.uuid4().hex}",
        is_publicly_listed=False,
        subscription_status="active",
        subscription_tier="growth",
    )
    db.add(tenant)
    db.flush()
    db.add(
        TenantCapabilities(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            can_create_tickets=True,
            can_access_analytics=True,
            can_upload_knowledge=True,
            max_conversations_per_month=1000,
        )
    )
    db.commit()
    db.refresh(tenant)
    return tenant


def make_user(db, tenant, *, role=UserRole.SUPPORT_AGENT, full_name="Test User") -> User:
    user = User(
        email=f"{uuid.uuid4().hex[:10]}@example.com",
        hashed_password="x",
        full_name=full_name,
        role=role,
        tenant_id=tenant.id,
        is_active=True,
        availability_status=AvailabilityStatus.AVAILABLE,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def headers(user) -> dict[str, str]:
    token = create_access_token(
        subject=str(user.id),
        additional_claims={"role": user.role.value, "tenant_id": str(user.tenant_id)},
    )
    return {"Authorization": f"Bearer {token}"}


def make_ticket(
    db, tenant, *, status=TicketStatus.IN_PROGRESS, assigned_to=None, resolved_at=None
) -> Ticket:
    session = ConversationSession(
        tenant_id=tenant.id,
        user_identifier=f"visitor-{uuid.uuid4().hex[:8]}",
        status=SessionStatus.ESCALATED,
        escalation_reason="Help",
        escalated_at=utcnow_naive(),
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    ticket = Ticket(
        tenant_id=tenant.id,
        session_id=session.id,
        ticket_number=f"TKT-N4-{uuid.uuid4().hex[:8]}",
        summary="Help",
        priority=TicketPriority.MEDIUM,
        status=status,
        assigned_to=assigned_to,
        resolved_at=resolved_at,
    )
    db.add(ticket)
    db.commit()
    db.refresh(ticket)
    return ticket


def make_rating(
    db,
    *,
    session_id,
    tenant_id,
    score=5,
    rated_agent_id=None,
    source=RatingSource.IDLE_PROMPT,
    rating_request_id=None,
    comment=None,
    meta=None,
    created_at=None,
    user_identifier="visitor",
) -> SessionRating:
    extra = {"created_at": created_at} if created_at is not None else {}
    rating = SessionRating(
        tenant_id=tenant_id,
        session_id=session_id,
        score=score,
        comment=comment,
        user_identifier=user_identifier,
        meta=meta,
        rated_agent_id=rated_agent_id,
        source=source,
        rating_request_id=rating_request_id,
        **extra,
    )
    db.add(rating)
    db.commit()
    db.refresh(rating)
    return rating


@pytest.fixture
def tenant(db):
    return make_tenant(db)


@pytest.fixture
def other_tenant(db):
    return make_tenant(db)


@pytest.fixture
def agent(db, tenant):
    return make_user(db, tenant, full_name="Sam Rivers")


@pytest.fixture
def other_agent(db, tenant):
    return make_user(db, tenant, full_name="Kim Lee")


@pytest.fixture
def admin(db, tenant):
    return make_user(db, tenant, role=UserRole.TENANT_ADMIN, full_name="Ada Admin")


@pytest.fixture
def viewer(db, tenant):
    return make_user(db, tenant, role=UserRole.VIEWER, full_name="Vic Viewer")


@pytest.fixture(autouse=True)
def fanout():
    """Every rating test runs with WS fanout mocked; tests that care inspect it."""
    mock = AsyncMock()
    with patch.object(connection_manager, "fanout_event", mock):
        yield mock


@pytest.fixture(autouse=True)
def _keep_fixtures_loaded(request):
    """The test `client` closes the shared session after each request (expunging every
    object); without expire-on-commit, fixtures stay readable for follow-up requests."""
    if "db" in request.fixturenames:
        request.getfixturevalue("db").expire_on_commit = False
    yield
