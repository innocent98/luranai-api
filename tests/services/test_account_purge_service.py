"""Tests for AccountPurgeService: the irreversible purge + tenant cascade.

New model (task-3-brief.md, revised): no retained legal/billing subset —
after the grace period, purge means purge. Covers: purge_user leaves no PII
for the user and is idempotent; tenant/platform-owned records that merely
reference the purged user (a ticket they were assigned, a session they
joined, platform feedback they submitted, ...) survive with the reference
de-identified rather than being deleted; purge_tenant hard-deletes every
tenant-scoped table (including what used to be a "retained" billing/audit
subset — there is no more such subset); and a PII-free DeletionRequest row
survives as the minimal proof a purge ran.
"""

from uuid import uuid4

import pytest

from app.core.encryption import hash_for_lookup
from app.db.models.agent_device import AgentDevice, DevicePlatform
from app.db.models.audit import AuditLog
from app.db.models.automation import TenantAction
from app.db.models.conversation import (
    ConversationSession,
    Message,
    MessageRole,
    SessionStatus,
    Ticket,
    TicketStatus,
)
from app.db.models.deletion_request import DeletionRequest, DeletionRequestedVia, DeletionScope
from app.db.models.flow import Flow, FlowTriggerType
from app.db.models.knowledge import KnowledgeChunk, KnowledgeSource, SourceType
from app.db.models.lead import Lead
from app.db.models.payment import PaymentTransaction
from app.db.models.refresh_token import RefreshToken
from app.db.models.review import PlatformFeedback, SessionRating
from app.db.models.sales_agent_config import TenantSalesAgentConfig
from app.db.models.subscription import SubscriptionHistory
from app.db.models.tenant import Tenant, TenantStatus
from app.db.models.user import DeletionStatus, User, UserRole
from app.db.models.visitor import Visitor, VisitorSource
from app.services.account_purge_service import AccountPurgeService
from app.utils.datetime import utcnow_aware

pytestmark = pytest.mark.usefixtures("db_session")


def _make_tenant(db_session) -> Tenant:
    unique = uuid4().hex[:8]
    tenant = Tenant(
        name=f"tenant_{unique}",
        display_name=f"Tenant {unique}",
        api_key_hash=uuid4().hex,
        subscription_tier="trial",
        subscription_status="trialing",
        status=TenantStatus.ACTIVE,
        is_publicly_listed=True,
    )
    db_session.add(tenant)
    db_session.flush()
    return tenant


def _make_user(
    db_session,
    tenant: Tenant,
    *,
    role: UserRole = UserRole.SUPPORT_AGENT,
    email: str | None = None,
) -> User:
    unique = uuid4().hex[:8]
    user = User(
        email=email or f"user_{unique}@example.com",
        hashed_password="$2b$12$placeholder_hash_not_real",
        full_name=f"Test User {unique}",
        role=role,
        tenant_id=tenant.id,
        is_active=True,
        deletion_status=DeletionStatus.ACTIVE,
    )
    db_session.add(user)
    db_session.flush()
    db_session.refresh(user)
    return user


def _make_session(db_session, tenant: Tenant, *, agent: User | None = None) -> ConversationSession:
    session = ConversationSession(
        tenant_id=tenant.id,
        user_identifier=f"visitor-{uuid4().hex[:8]}",
        status=SessionStatus.ACTIVE,
        last_message_at=utcnow_aware(),
        agent_id=agent.id if agent else None,
    )
    db_session.add(session)
    db_session.flush()
    return session


def _make_ticket(db_session, tenant: Tenant, session: ConversationSession, agent: User) -> Ticket:
    ticket = Ticket(
        tenant_id=tenant.id,
        session_id=session.id,
        ticket_number=f"TKT-{uuid4().hex[:10]}",
        status=TicketStatus.OPEN,
        summary="Test escalation",
        assigned_to=agent.id,
    )
    db_session.add(ticket)
    db_session.flush()
    return ticket


def _make_refresh_token(db_session, user: User) -> RefreshToken:
    record = RefreshToken(
        user_id=user.id,
        user_type="tenant_user",
        token_hash=uuid4().hex,
        expires_at=utcnow_aware(),
        created_at=utcnow_aware(),
    )
    db_session.add(record)
    db_session.flush()
    return record


def _make_agent_device(db_session, tenant: Tenant, user: User) -> AgentDevice:
    device = AgentDevice(
        user_id=user.id,
        tenant_id=tenant.id,
        device_token=uuid4().hex,
        platform=DevicePlatform.ANDROID,
    )
    db_session.add(device)
    db_session.flush()
    return device


def _make_grace_request(
    db_session, user: User, *, scope=DeletionScope.SELF_USER
) -> DeletionRequest:
    request = DeletionRequest(
        user_id=user.id,
        tenant_id=user.tenant_id,
        scope=scope,
        status=DeletionStatus.GRACE,
        requested_via=DeletionRequestedVia.AUTHED,
        cancel_token_hash=uuid4().hex,
    )
    db_session.add(request)
    db_session.flush()
    return request


def _make_payment_transaction(db_session, tenant: Tenant) -> PaymentTransaction:
    txn = PaymentTransaction(
        tenant_id=tenant.id,
        reference=f"ref_{uuid4().hex[:12]}",
        amount=500000,
        tier="professional",
        billing_cycle="monthly",
        customer_email="billing-contact@example.com",
    )
    db_session.add(txn)
    db_session.flush()
    return txn


def _make_knowledge_source(db_session, tenant: Tenant) -> KnowledgeSource:
    source = KnowledgeSource(
        tenant_id=tenant.id,
        name="Test doc",
        source_type=SourceType.FAQ,
        content_hash=uuid4().hex,
    )
    db_session.add(source)
    db_session.flush()
    return source


# ---------------------------------------------------------------------------
# purge_user — (a) no PII survives, (b) idempotent
# ---------------------------------------------------------------------------


def test_purge_user_scrubs_all_pii_and_tombstones_row(db_session):
    tenant = _make_tenant(db_session)
    agent = _make_user(db_session, tenant, email="agent@example.com")
    original_email_hash = agent.email_hash
    refresh_token = _make_refresh_token(db_session, agent)
    device = _make_agent_device(db_session, tenant, agent)

    service = AccountPurgeService(db_session)
    service.purge_user(agent.id)

    db_session.refresh(agent)
    assert agent.deletion_status == DeletionStatus.PURGED
    assert agent.is_active is False
    assert agent.full_name == "Deleted user"
    assert agent.email != "agent@example.com"
    assert agent.email.endswith("@purged.invalid")
    assert agent.email_hash != original_email_hash
    assert agent.hashed_password != "$2b$12$placeholder_hash_not_real"

    # Row survives as a PII-free shell — id/tenant_id unchanged.
    assert db_session.get(User, agent.id) is not None
    assert agent.tenant_id == tenant.id

    # Purely-the-user's-own artifacts are gone, not just detached.
    assert db_session.get(RefreshToken, refresh_token.id) is None
    assert db_session.get(AgentDevice, device.id) is None


def test_purge_user_is_idempotent(db_session):
    tenant = _make_tenant(db_session)
    agent = _make_user(db_session, tenant)
    service = AccountPurgeService(db_session)

    service.purge_user(agent.id)
    db_session.refresh(agent)
    email_after_first = agent.email
    email_hash_after_first = agent.email_hash

    service.purge_user(agent.id)  # must not raise
    db_session.refresh(agent)

    assert agent.deletion_status == DeletionStatus.PURGED
    assert agent.email == email_after_first
    assert agent.email_hash == email_hash_after_first


def test_purge_user_unknown_id_is_a_noop(db_session):
    service = AccountPurgeService(db_session)

    service.purge_user(uuid4())  # must not raise


def test_purge_user_frees_original_email_hash_for_reuse(db_session):
    tenant = _make_tenant(db_session)
    original_email = f"reusable-{uuid4().hex[:8]}@example.com"
    agent = _make_user(db_session, tenant, email=original_email)
    service = AccountPurgeService(db_session)

    service.purge_user(agent.id)
    db_session.refresh(agent)

    assert agent.email_hash != hash_for_lookup(original_email)

    # A fresh signup with the same original email must not collide on the
    # unique email_hash constraint — proving the address was actually freed.
    new_user = _make_user(db_session, tenant, email=original_email)
    assert new_user.email_hash == hash_for_lookup(original_email)


# ---------------------------------------------------------------------------
# purge_user — (c) tenant-owned ticket survives, de-identified
# ---------------------------------------------------------------------------


def test_purge_user_deidentifies_assigned_ticket_without_deleting_it(db_session):
    tenant = _make_tenant(db_session)
    agent = _make_user(db_session, tenant)
    session = _make_session(db_session, tenant)
    ticket = _make_ticket(db_session, tenant, session, agent)

    service = AccountPurgeService(db_session)
    service.purge_user(agent.id)

    db_session.refresh(ticket)
    assert ticket.assigned_to is None
    # The tenant's support record itself is untouched.
    assert ticket.summary == "Test escalation"
    assert ticket.status == TicketStatus.OPEN
    assert db_session.get(ConversationSession, session.id) is not None


# ---------------------------------------------------------------------------
# purge_user — (d) authored message survives, author de-identified
# ---------------------------------------------------------------------------


def test_purge_user_deidentifies_session_agent_message_content_survives(db_session):
    tenant = _make_tenant(db_session)
    agent = _make_user(db_session, tenant)
    session = _make_session(db_session, tenant, agent=agent)
    message = Message(session_id=session.id, role=MessageRole.HUMAN_AGENT, content="On it!")
    db_session.add(message)
    db_session.flush()
    message_id = message.id

    service = AccountPurgeService(db_session)
    service.purge_user(agent.id)

    db_session.refresh(session)
    assert session.agent_id is None  # de-identified

    surviving_message = db_session.get(Message, message_id)
    assert surviving_message is not None
    assert surviving_message.content == "On it!"  # customer-support content preserved
    assert surviving_message.role == MessageRole.HUMAN_AGENT


def test_purge_user_deidentifies_other_tenant_owned_fk_references(db_session):
    """Broader sweep coverage beyond the two brief-mandated cases above:
    Flow.created_by and PlatformFeedback.submitted_by are two of the other
    13 inbound FKs to users.id — both must survive nulled, not deleted."""
    tenant = _make_tenant(db_session)
    agent = _make_user(db_session, tenant, role=UserRole.TENANT_ADMIN)

    flow = Flow(
        tenant_id=tenant.id,
        name="Welcome flow",
        trigger_type=FlowTriggerType.MANUAL,
        created_by=agent.id,
    )
    db_session.add(flow)

    feedback = PlatformFeedback(
        tenant_id=tenant.id,
        category="general",
        title="Great product",
        description="Loving it so far.",
        submitted_by=agent.id,
    )
    db_session.add(feedback)
    db_session.flush()
    flow_id, feedback_id = flow.id, feedback.id

    service = AccountPurgeService(db_session)
    service.purge_user(agent.id)

    db_session.refresh(flow)
    db_session.refresh(feedback)
    assert flow.created_by is None
    assert flow.name == "Welcome flow"
    assert feedback.submitted_by is None
    assert feedback.title == "Great product"
    assert db_session.get(Flow, flow_id) is not None
    assert db_session.get(PlatformFeedback, feedback_id) is not None


# ---------------------------------------------------------------------------
# purge_user / purge_tenant — (f) PII-free deletion audit survives
# ---------------------------------------------------------------------------


def test_purge_user_marks_deletion_request_purged_without_deleting_it(db_session):
    tenant = _make_tenant(db_session)
    agent = _make_user(db_session, tenant)
    request = _make_grace_request(db_session, agent)
    request_id = request.id

    service = AccountPurgeService(db_session)
    service.purge_user(agent.id)

    surviving_request = db_session.get(DeletionRequest, request_id)
    assert surviving_request is not None
    assert surviving_request.status == DeletionStatus.PURGED
    # Still resolves to the (now PII-free) tombstone — no dangling FK.
    assert db_session.get(User, surviving_request.user_id) is not None


# ---------------------------------------------------------------------------
# purge_tenant — (e) full cascade delete of all tenant-scoped data + members
# ---------------------------------------------------------------------------


def test_purge_tenant_hard_deletes_all_tenant_scoped_data_and_purges_members(db_session):
    tenant = _make_tenant(db_session)
    admin = _make_user(db_session, tenant, role=UserRole.TENANT_ADMIN)
    agent = _make_user(db_session, tenant, role=UserRole.SUPPORT_AGENT)
    tenant_request = _make_grace_request(db_session, admin, scope=DeletionScope.TENANT)

    session = _make_session(db_session, tenant, agent=agent)
    ticket = _make_ticket(db_session, tenant, session, agent)
    message = Message(session_id=session.id, role=MessageRole.USER, content="hello")
    db_session.add(message)

    lead = Lead(tenant_id=tenant.id, email="lead@example.com", name="Lead Name")
    db_session.add(lead)

    source = _make_knowledge_source(db_session, tenant)
    chunk = KnowledgeChunk(
        tenant_id=tenant.id,
        source_id=source.id,
        content="chunk text",
        chunk_index=0,
        vector_id=uuid4().hex,
    )
    db_session.add(chunk)

    sales_config = TenantSalesAgentConfig(tenant_id=tenant.id)
    db_session.add(sales_config)

    device = _make_agent_device(db_session, tenant, agent)

    visitor = Visitor(
        tenant_id=tenant.id,
        public_token=uuid4().hex,
        email="visitor@example.com",
        first_seen_at=utcnow_aware(),
        last_seen_at=utcnow_aware(),
        created_from=VisitorSource.WIDGET,
    )
    db_session.add(visitor)

    rating = SessionRating(
        tenant_id=tenant.id, session_id=session.id, score=5, user_identifier="u1"
    )
    db_session.add(rating)

    # These used to be a "retained legal/billing subset" — under the new
    # model they are hard-deleted along with everything else.
    txn = _make_payment_transaction(db_session, tenant)
    history = SubscriptionHistory(
        tenant_id=tenant.id, action="upgrade", changed_by_type="tenant_admin"
    )
    db_session.add(history)
    audit_log = AuditLog(tenant_id=tenant.id, action="test_action", resource_type="test")
    db_session.add(audit_log)
    automation = TenantAction(
        tenant_id=tenant.id,
        name="Send webhook",
        slug=f"send-webhook-{uuid4().hex[:8]}",
        method="POST",
        url_template="https://example.com",
    )
    db_session.add(automation)

    db_session.flush()
    session_id, ticket_id, message_id = session.id, ticket.id, message.id
    lead_id, source_id, chunk_id = lead.id, source.id, chunk.id
    sales_config_id, device_id, visitor_id = sales_config.id, device.id, visitor.id
    rating_id, txn_id, history_id = rating.id, txn.id, history.id
    audit_log_id, automation_id = audit_log.id, automation.id

    service = AccountPurgeService(db_session)
    service.purge_tenant(tenant.id)

    # Every tenant-scoped table the cascade claims to cover is gone.
    for model, row_id in (
        (ConversationSession, session_id),
        (Ticket, ticket_id),
        (Message, message_id),
        (Lead, lead_id),
        (KnowledgeSource, source_id),
        (KnowledgeChunk, chunk_id),
        (TenantSalesAgentConfig, sales_config_id),
        (AgentDevice, device_id),
        (Visitor, visitor_id),
        (SessionRating, rating_id),
        (PaymentTransaction, txn_id),
        (SubscriptionHistory, history_id),
        (AuditLog, audit_log_id),
        (TenantAction, automation_id),
    ):
        assert db_session.get(model, row_id) is None, f"{model.__name__} row survived purge_tenant"

    # Tenant row survives as a PII-free shell (deletion_requests.tenant_id
    # is NOT NULL + CASCADE — see module docstring).
    surviving_tenant = db_session.get(Tenant, tenant.id)
    assert surviving_tenant is not None
    assert surviving_tenant.status == TenantStatus.INACTIVE
    assert surviving_tenant.is_publicly_listed is False
    assert surviving_tenant.display_name == "Deleted tenant"

    # Members purged.
    db_session.refresh(admin)
    db_session.refresh(agent)
    assert admin.deletion_status == DeletionStatus.PURGED
    assert admin.full_name == "Deleted user"
    assert agent.deletion_status == DeletionStatus.PURGED
    assert agent.full_name == "Deleted user"

    # Minimal PII-free audit trail survives.
    surviving_request = db_session.get(DeletionRequest, tenant_request.id)
    assert surviving_request is not None
    assert surviving_request.status == DeletionStatus.PURGED


def test_purge_tenant_is_idempotent(db_session):
    tenant = _make_tenant(db_session)
    _make_user(db_session, tenant, role=UserRole.TENANT_ADMIN)
    service = AccountPurgeService(db_session)

    service.purge_tenant(tenant.id)
    service.purge_tenant(tenant.id)  # must not raise

    surviving_tenant = db_session.get(Tenant, tenant.id)
    assert surviving_tenant.status == TenantStatus.INACTIVE


def test_purge_tenant_unknown_id_is_a_noop(db_session):
    service = AccountPurgeService(db_session)

    service.purge_tenant(uuid4())  # must not raise


def test_purge_tenant_does_not_touch_other_tenants_data(db_session):
    tenant_a = _make_tenant(db_session)
    tenant_b = _make_tenant(db_session)
    _make_user(db_session, tenant_a, role=UserRole.TENANT_ADMIN)
    user_b = _make_user(db_session, tenant_b, role=UserRole.TENANT_ADMIN)
    lead_b = Lead(tenant_id=tenant_b.id, email="other-tenant-lead@example.com")
    db_session.add(lead_b)
    db_session.flush()
    lead_b_id = lead_b.id

    service = AccountPurgeService(db_session)
    service.purge_tenant(tenant_a.id)

    db_session.refresh(user_b)
    assert user_b.deletion_status == DeletionStatus.ACTIVE
    assert db_session.get(Lead, lead_b_id) is not None
    surviving_tenant_b = db_session.get(Tenant, tenant_b.id)
    assert surviving_tenant_b.status == TenantStatus.ACTIVE
