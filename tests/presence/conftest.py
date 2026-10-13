"""Fixtures for customer-presence tests (reuses the agent_caller tenant/user fixtures)."""

import time
import uuid

import pytest

from app.core.security import create_access_token
from app.db.models.conversation import (
    ConversationSession,
    SessionStatus,
    Ticket,
    TicketPriority,
    TicketStatus,
)
from app.db.models.tenant import Tenant, TenantStatus
from app.db.models.user import User, UserRole
from app.services import customer_presence as cp
from app.utils.datetime import utcnow_naive
from tests.agent_caller.conftest import (  # noqa: F401  (fixtures)
    admin_user,
    agent_tenant,
    agent_user,
    auth_headers,
    viewer_user,
)


def seed_presence(session_id, state: str, *, ttl: float = 60) -> None:
    """Write a live connection entry exactly as the tracker does (contract-level seed)."""
    cp._get_sync_client().zadd(
        cp._conn_key(session_id), {f"seed-{uuid.uuid4().hex[:6]}|{state}": time.time() + ttl}
    )


def make_escalated(db, tenant, *, status=TicketStatus.OPEN):
    session = ConversationSession(
        tenant_id=tenant.id,
        user_identifier=f"visitor:{uuid.uuid4().hex[:8]}",
        status=SessionStatus.ESCALATED,
        escalation_reason="Refund",
        escalated_at=utcnow_naive(),
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    ticket = Ticket(
        tenant_id=tenant.id,
        session_id=session.id,
        ticket_number=f"TKT-{uuid.uuid4().hex[:8]}",
        summary="Refund",
        priority=TicketPriority.HIGH,
        status=status,
    )
    db.add(ticket)
    db.commit()
    db.refresh(ticket)
    return session, ticket


@pytest.fixture
def other_tenant_headers(db):
    tenant = Tenant(
        name=f"other-{uuid.uuid4().hex[:6]}",
        display_name="Other Co",
        status=TenantStatus.ACTIVE,
        api_key_hash=f"hash-{uuid.uuid4().hex}",
        subscription_status="active",
        subscription_tier="growth",
    )
    db.add(tenant)
    db.commit()
    user = User(
        email="x@other.com",
        hashed_password="x",
        full_name="X",
        role=UserRole.TENANT_ADMIN,
        tenant_id=tenant.id,
        is_active=True,
    )
    db.add(user)
    db.commit()
    token = create_access_token(
        subject=str(user.id),
        additional_claims={"role": user.role.value, "tenant_id": str(user.tenant_id)},
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def admin_headers(admin_user):  # noqa: F811
    token = create_access_token(
        subject=str(admin_user.id),
        additional_claims={"role": admin_user.role.value, "tenant_id": str(admin_user.tenant_id)},
    )
    return {"Authorization": f"Bearer {token}"}
