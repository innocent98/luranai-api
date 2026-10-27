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

from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy import func, select

import app.db.models  # noqa: F401  — ensures every model is registered on Base.metadata
from app.core.encryption import hash_for_lookup
from app.db.base import Base
from app.db.models.agent_device import AgentDevice, DevicePlatform
from app.db.models.api_key import APIKey, APIKeyType
from app.db.models.audit import AnalyticsMetric, AuditEntry, AuditLog, AuditReport, AuditScope
from app.db.models.automation import (
    ActionExecution,
    ActionSecret,
    TenantAction,
    TenantAutomationConfig,
)
from app.db.models.business_profile import BusinessProfile
from app.db.models.conversation import (
    AttachmentDirection,
    AttachmentModeration,
    AttachmentStatus,
    ConversationSession,
    MessageAttachment,
    Message,
    MessageRole,
    SessionStatus,
    Ticket,
    TicketStatus,
)
from app.db.models.customer_engagement import CustomerBusinessFollow, TenantChannelPost
from app.db.models.customer_user import CustomerUser
from app.db.models.deletion_request import DeletionRequest, DeletionRequestedVia, DeletionScope
from app.db.models.email_notification import EmailNotification
from app.db.models.flow import Flow, FlowTriggerType
from app.db.models.job import JobType, ProcessingJob
from app.db.models.knowledge import KnowledgeChunk, KnowledgeSource, SourceType
from app.db.models.lead import Lead
from app.db.models.meeting import Meeting
from app.db.models.notification_log import NotifChannel, NotificationLog, NotifEvent
from app.db.models.payment import PaymentTransaction
from app.db.models.promotional_grant import PromotionalGrant
from app.db.models.quote import Quote
from app.db.models.refresh_token import RefreshToken
from app.db.models.review import PlatformFeedback, RatingRequest, SessionRating
from app.db.models.sales_agent_config import TenantSalesAgentConfig
from app.db.models.subscription import SubscriptionHistory
from app.db.models.tenant import Tenant, TenantAIConfig, TenantCapabilities, TenantStatus
from app.db.models.user import DeletionStatus, User, UserRole
from app.db.models.visit_analytics import DailyVisit
from app.db.models.whatsapp import WhatsAppChat, WhatsAppConnection, WhatsAppInboundMessage
from app.db.models.visitor import Visitor, VisitorSession, VisitorSource
from app.services.account_purge_service import AccountPurgeService
from app.utils.datetime import utcnow_aware

pytestmark = pytest.mark.usefixtures("db_session")

# Tables intentionally NOT wiped by purge_tenant — see
# app/services/account_purge_service.py's module docstring for why each is
# kept. Used by the completeness guard test below.
_PURGE_TENANT_ALLOWLIST = {
    "deletion_requests",  # the PII-free audit trail proving a purge ran
    "platform_email_campaigns",  # Luran's own outbound record; tenant_id
    "platform_email_campaign_recipients",  # is ondelete=SET NULL, not
    # CASCADE — deliberately designed to survive a tenant's deletion.
    "users",  # kept as a purged shell, not deleted — checked separately
    # below (every row must be PURGED, not that zero rows remain).
}


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


def test_purge_user_keeps_rating_attribution_and_nulls_requested_by(db_session):
    """N4-D10: NULLing rated_agent_id would turn a human rating into an AI one (and could
    collide with the AI partial unique index); the purged user row is a PII-free shell,
    so the attribution FKs are kept. requested_by is a "who did it" column -> NULL."""
    tenant = _make_tenant(db_session)
    agent = _make_user(db_session, tenant)
    admin = _make_user(db_session, tenant, role=UserRole.TENANT_ADMIN)
    session = _make_session(db_session, tenant, agent=agent)
    request = RatingRequest(
        tenant_id=tenant.id,
        session_id=session.id,
        agent_id=agent.id,
        requested_by=admin.id,
        source="manual_request",
        token_hash=uuid4().hex + uuid4().hex,
        expires_at=utcnow_aware() + timedelta(days=7),
    )
    db_session.add(request)
    db_session.flush()
    rating = SessionRating(
        tenant_id=tenant.id,
        session_id=session.id,
        score=4,
        user_identifier="u1",
        rated_agent_id=agent.id,
        source="manual_request",
        rating_request_id=request.id,
    )
    db_session.add(rating)
    db_session.flush()

    service = AccountPurgeService(db_session)
    service.purge_user(agent.id)
    service.purge_user(admin.id)

    db_session.refresh(rating)
    db_session.refresh(request)
    assert rating.rated_agent_id == agent.id
    assert request.agent_id == agent.id
    assert request.requested_by is None


def test_purge_user_scrubs_audit_log_ip_and_user_agent(db_session):
    """Fix round 1: user_id was already nulled, but ip_address/user_agent
    (GDPR personal data) were left denormalized on the same row."""
    tenant = _make_tenant(db_session)
    agent = _make_user(db_session, tenant)
    log = AuditLog(
        tenant_id=tenant.id,
        user_id=agent.id,
        action="login",
        resource_type="session",
        ip_address="203.0.113.42",
        user_agent="Mozilla/5.0 (test)",
    )
    db_session.add(log)
    db_session.flush()
    log_id = log.id

    service = AccountPurgeService(db_session)
    service.purge_user(agent.id)

    surviving_log = db_session.get(AuditLog, log_id)
    assert surviving_log is not None  # the audit record itself survives
    assert surviving_log.user_id is None
    assert surviving_log.ip_address is None
    assert surviving_log.user_agent is None
    assert surviving_log.action == "login"  # non-PII content preserved


def test_purge_user_scrubs_subscription_history_changed_by_name(db_session):
    """Fix round 1: closes the gap flagged in the original submission —
    changed_by_id has no declared FK to users.id, so it's found by matching
    on the id directly rather than the grep-driven FK sweep."""
    tenant = _make_tenant(db_session)
    admin = _make_user(db_session, tenant, role=UserRole.TENANT_ADMIN)
    history = SubscriptionHistory(
        tenant_id=tenant.id,
        action="upgrade",
        changed_by_type="tenant_admin",
        changed_by_id=admin.id,
        changed_by_name="Test Admin",
    )
    db_session.add(history)
    db_session.flush()
    history_id = history.id

    service = AccountPurgeService(db_session)
    service.purge_user(admin.id)

    surviving_history = db_session.get(SubscriptionHistory, history_id)
    assert surviving_history is not None  # the billing audit row survives
    assert surviving_history.changed_by_name is None
    assert surviving_history.action == "upgrade"  # non-PII content preserved


def test_purge_user_scrubs_email_notification_recipient_and_template_data(db_session):
    """Fix round 2: email_notifications has no users.id FK at all — it's
    matched by tenant_id + the user's original email, which is why this
    must run before _anonymize_user overwrites .email."""
    tenant = _make_tenant(db_session)
    admin = _make_user(db_session, tenant, role=UserRole.TENANT_ADMIN, email="admin@example.com")
    notification = EmailNotification(
        tenant_id=tenant.id,
        notification_type="trial_expiring",
        recipient_email="admin@example.com",
        subject="Your trial is ending",
        template_data={"admin_name": "Admin Full Name", "company_name": "Acme"},
    )
    db_session.add(notification)
    db_session.flush()
    notification_id = notification.id

    service = AccountPurgeService(db_session)
    service.purge_user(admin.id)

    surviving = db_session.get(EmailNotification, notification_id)
    assert surviving is not None  # the send/audit record itself survives
    assert surviving.recipient_email != "admin@example.com"
    assert surviving.recipient_email.endswith("@purged.invalid")
    assert surviving.template_data is None
    assert surviving.subject == "Your trial is ending"  # non-PII content preserved


def test_purge_user_scrubs_notification_log_recipient(db_session):
    """Fix round 2: notification_logs.recipient is the same class of gap —
    keyed by tenant_id + the user's original email, no users.id FK."""
    tenant = _make_tenant(db_session)
    agent = _make_user(db_session, tenant, email="rep@example.com")
    log = NotificationLog(
        tenant_id=tenant.id,
        event=NotifEvent.SALES_READY_LEAD,
        channel=NotifChannel.EMAIL,
        entity_type="lead",
        entity_id=uuid4(),
        recipient="rep@example.com",
        title="New sales-ready lead",
        body="Jane Doe · Acme Corp · score 80",
    )
    db_session.add(log)
    db_session.flush()
    log_id = log.id

    service = AccountPurgeService(db_session)
    service.purge_user(agent.id)

    surviving = db_session.get(NotificationLog, log_id)
    assert surviving is not None  # the notification record itself survives
    assert surviving.recipient != "rep@example.com"
    assert surviving.recipient.endswith("@purged.invalid")
    # title/body describe the lead, not the recipient — no PII of the
    # purged user in either, left untouched deliberately (see docstring).
    assert surviving.title == "New sales-ready lead"
    assert surviving.body == "Jane Doe · Acme Corp · score 80"


def test_purge_user_does_not_scrub_another_tenants_matching_email(db_session):
    """The match is tenant_id + email, not email alone — a coincidentally
    identical recipient address in a different tenant must survive."""
    tenant_a = _make_tenant(db_session)
    tenant_b = _make_tenant(db_session)
    agent_a = _make_user(db_session, tenant_a, email="shared@example.com")
    # Note: User.email_hash is globally unique, so tenant_b intentionally
    # has no User row with this address — the point is that the
    # EmailNotification match key is (tenant_id, recipient_email), not a
    # second real user, so a same-looking recipient row that merely
    # belongs to a different tenant must still survive untouched.
    other_tenant_notification = EmailNotification(
        tenant_id=tenant_b.id,
        notification_type="trial_expiring",
        recipient_email="shared@example.com",
        subject="hi",
    )
    db_session.add(other_tenant_notification)
    db_session.flush()
    other_id = other_tenant_notification.id

    service = AccountPurgeService(db_session)
    service.purge_user(agent_a.id)

    surviving = db_session.get(EmailNotification, other_id)
    assert surviving is not None
    assert surviving.recipient_email == "shared@example.com"


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

    # Missing from the original delete list — fix round 1.
    email_notification = EmailNotification(
        tenant_id=tenant.id,
        notification_type="trial_expiring",
        recipient_email="owner@example.com",
        subject="Your trial is ending",
    )
    db_session.add(email_notification)
    platform_feedback = PlatformFeedback(
        tenant_id=tenant.id,
        category="general",
        title="Feature request",
        description="Would love a dark mode.",
        submitted_by=agent.id,
    )
    db_session.add(platform_feedback)

    db_session.flush()
    session_id, ticket_id, message_id = session.id, ticket.id, message.id
    lead_id, source_id, chunk_id = lead.id, source.id, chunk.id
    sales_config_id, device_id, visitor_id = sales_config.id, device.id, visitor.id
    rating_id, txn_id, history_id = rating.id, txn.id, history.id
    audit_log_id, automation_id = audit_log.id, automation.id
    email_notification_id, platform_feedback_id = email_notification.id, platform_feedback.id

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
        (EmailNotification, email_notification_id),
        (PlatformFeedback, platform_feedback_id),
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


# ---------------------------------------------------------------------------
# Completeness guard — the test that would have caught the original miss.
#
# purge_tenant's explicit per-table delete list is the ONLY thing that
# removes tenant data (the Tenant row is kept as a shell, so its
# ondelete=CASCADE FKs never fire). This test reflects every table with a
# declared tenant_id -> tenants.id FK straight off Base.metadata — not off
# the service's own delete list — and asserts zero surviving rows for the
# purged tenant in each, except the explicit allowlist above. A future
# tenant-scoped table that forgets to get added to the service's delete
# list fails THIS test instead of silently leaking PII.
# ---------------------------------------------------------------------------


def _tenant_scoped_tables():
    """Every table with a `tenant_id` column FK'd to `tenants.id`."""
    tables = []
    for table in Base.metadata.tables.values():
        column = table.columns.get("tenant_id")
        if column is None:
            continue
        if any(fk.column.table.name == "tenants" for fk in column.foreign_keys):
            tables.append(table)
    return tables


def _make_attachment(tenant_id, session_id, public_id: str | None = None) -> MessageAttachment:
    return MessageAttachment(
        tenant_id=tenant_id,
        session_id=session_id,
        direction=AttachmentDirection.INBOUND,
        cloudinary_public_id=public_id or f"luran/chat/{uuid4().hex}",
        url="https://res.cloudinary.com/demo/image/upload/x.jpg",
        content_type="image/jpeg",
        bytes=1234,
        moderation_status=AttachmentModeration.PASSED,
        status=AttachmentStatus.UPLOADED,
    )


def _seed_tenant_with_attachments(db_session, public_ids):
    tenant = _make_tenant(db_session)
    session = ConversationSession(
        tenant_id=tenant.id,
        user_identifier=f"visitor-{uuid4().hex[:8]}",
        status=SessionStatus.ACTIVE,
        last_message_at=utcnow_aware(),
    )
    db_session.add(session)
    db_session.flush()
    for pid in public_ids:
        db_session.add(_make_attachment(tenant.id, session.id, pid))
    db_session.flush()
    return tenant


def _attachment_count(db_session, tenant_id) -> int:
    return db_session.execute(
        select(func.count())
        .select_from(MessageAttachment)
        .where(MessageAttachment.tenant_id == tenant_id)
    ).scalar_one()


def test_purge_tenant_deletes_attachments_and_cloudinary_assets(db_session):
    pids = [f"luran/chat/{uuid4().hex}", f"luran/chat/{uuid4().hex}"]
    tenant = _seed_tenant_with_attachments(db_session, pids)

    with patch(
        "app.services.account_purge_service.cloudinary_manager.destroy",
        return_value=(True, None),
    ) as destroy:
        AccountPurgeService(db_session).purge_tenant(tenant.id)

    assert destroy.call_count == 2
    assert {c.args[0] for c in destroy.call_args_list} == set(pids)
    assert all(c.kwargs == {"resource_type": "image"} for c in destroy.call_args_list)
    assert _attachment_count(db_session, tenant.id) == 0


@pytest.mark.parametrize("failure", ["returns_false", "raises"])
def test_purge_tenant_survives_cloudinary_destroy_failure(db_session, failure):
    pids = [f"luran/chat/{uuid4().hex}", f"luran/chat/{uuid4().hex}"]
    tenant = _seed_tenant_with_attachments(db_session, pids)

    def flaky(public_id, *, resource_type):
        if public_id == pids[0]:
            if failure == "raises":
                raise RuntimeError("cloudinary down")
            return False, "Failed to delete image"
        return True, None

    with patch(
        "app.services.account_purge_service.cloudinary_manager.destroy", side_effect=flaky
    ) as destroy:
        AccountPurgeService(db_session).purge_tenant(tenant.id)

    assert destroy.call_count == 2  # the other asset was still attempted
    assert _attachment_count(db_session, tenant.id) == 0
    assert db_session.get(Tenant, tenant.id) is not None


def _seed_every_tenant_scoped_table(db_session, tenant: Tenant, agent: User) -> set[str]:
    """Put at least one row for `tenant` in every tenant-scoped table that
    isn't already covered by another test's fixtures, so the completeness
    guard below actually exercises every table instead of vacuously
    passing on tables nobody ever put a row in. Returns the set of table
    names seeded, for a sanity check against the full discovered list."""
    seeded: set[str] = set()

    def _add(obj, table_name: str):
        db_session.add(obj)
        db_session.flush()
        seeded.add(table_name)

    _add(
        ConversationSession(
            tenant_id=tenant.id,
            user_identifier=f"visitor-{uuid4().hex[:8]}",
            status=SessionStatus.ACTIVE,
            last_message_at=utcnow_aware(),
        ),
        "conversation_sessions",
    )
    lead = Lead(tenant_id=tenant.id, email="lead@example.com")
    _add(lead, "leads")
    _add(
        Quote(tenant_id=tenant.id, lead_id=lead.id, line_items=[], total=0),
        "quotes",
    )
    _add(
        Meeting(tenant_id=tenant.id, lead_id=lead.id, scheduled_at=utcnow_aware()),
        "meetings",
    )
    source = KnowledgeSource(
        tenant_id=tenant.id, name="doc", source_type=SourceType.FAQ, content_hash=uuid4().hex
    )
    _add(source, "knowledge_sources")
    _add(
        KnowledgeChunk(
            tenant_id=tenant.id,
            source_id=source.id,
            content="text",
            chunk_index=0,
            vector_id=uuid4().hex,
        ),
        "knowledge_chunks",
    )
    _add(TenantSalesAgentConfig(tenant_id=tenant.id), "tenant_sales_agent_config")
    _add(
        AgentDevice(
            user_id=agent.id,
            tenant_id=tenant.id,
            device_token=uuid4().hex,
            platform=DevicePlatform.ANDROID,
        ),
        "agent_devices",
    )
    _add(
        APIKey(
            tenant_id=tenant.id,
            key_type=APIKeyType.SECRET,
            key_prefix="sk_test",
            key_hash=uuid4().hex,
            name="test key",
        ),
        "api_keys",
    )
    _add(BusinessProfile(tenant_id=tenant.id), "business_profiles")
    visitor = Visitor(
        tenant_id=tenant.id,
        public_token=uuid4().hex,
        first_seen_at=utcnow_aware(),
        last_seen_at=utcnow_aware(),
        created_from=VisitorSource.WIDGET,
    )
    _add(visitor, "visitors")
    wa_connection = WhatsAppConnection(
        tenant_id=tenant.id,
        status="connected",
        phone_e164="2348000000001",
        phone_hmac=uuid4().hex + uuid4().hex,
        session_key=f"sk_{uuid4().hex}",
        consent_version="v",
        consent_accepted_at=utcnow_aware(),
    )
    _add(wa_connection, "whatsapp_connections")
    _add(
        WhatsAppChat(
            tenant_id=tenant.id,
            connection_id=wa_connection.id,
            contact_hmac=uuid4().hex + uuid4().hex,
            contact_ref="198765432100@lid",
            visitor_id=visitor.id,
        ),
        "whatsapp_chats",
    )
    db_session.add(
        WhatsAppInboundMessage(
            connection_id=wa_connection.id,
            provider_message_id="PURGE-GUARD",
            received_at=utcnow_aware(),
        )
    )
    _add(
        VisitorSession(
            visitor_id=visitor.id,
            tenant_id=tenant.id,
            session_token=uuid4().hex,
            started_at=utcnow_aware(),
            last_activity_at=utcnow_aware(),
            entry_point=VisitorSource.WIDGET,
            created_at=utcnow_aware(),
        ),
        "visitor_sessions",
    )
    _add(
        SessionRating(
            tenant_id=tenant.id,
            session_id=(
                db_session.execute(
                    select(ConversationSession.id).where(ConversationSession.tenant_id == tenant.id)
                )
                .scalars()
                .first()
            ),
            score=5,
            user_identifier="u1",
        ),
        "session_ratings",
    )
    _add(
        RatingRequest(
            tenant_id=tenant.id,
            session_id=(
                db_session.execute(
                    select(ConversationSession.id).where(ConversationSession.tenant_id == tenant.id)
                )
                .scalars()
                .first()
            ),
            agent_id=agent.id,
            source="manual_request",
            token_hash=uuid4().hex + uuid4().hex,
            expires_at=utcnow_aware() + timedelta(days=7),
        ),
        "rating_requests",
    )
    _add(_make_payment_transaction(db_session, tenant), "payment_transactions")
    _add(
        SubscriptionHistory(tenant_id=tenant.id, action="upgrade", changed_by_type="system"),
        "subscription_history",
    )
    _add(
        AuditLog(tenant_id=tenant.id, action="test", resource_type="test"),
        "audit_logs",
    )
    _add(
        AnalyticsMetric(
            tenant_id=tenant.id,
            metric_type="conversation_count",
            metric_value=1.0,
            period_start=utcnow_aware(),
            period_end=utcnow_aware(),
        ),
        "analytics_metrics",
    )
    report = AuditReport(
        tenant_id=tenant.id,
        url="https://example.com",
        domain="example.com",
        scope=AuditScope.DASHBOARD,
    )
    _add(report, "audit_reports")
    _add(
        AuditEntry(
            audit_report_id=report.id,
            tenant_id=tenant.id,
            url="https://example.com/page",
            content="page content",
            content_hash=uuid4().hex,
        ),
        "audit_entries",
    )
    _add(
        ProcessingJob(tenant_id=tenant.id, job_type=JobType.DOCUMENT_INGESTION, input_data={}),
        "processing_jobs",
    )
    action = TenantAction(
        tenant_id=tenant.id,
        name="Send webhook",
        slug=f"webhook-{uuid4().hex[:8]}",
        method="POST",
        url_template="https://example.com",
    )
    _add(action, "tenant_actions")
    _add(
        ActionExecution(
            tenant_id=tenant.id, action_id=action.id, input_payload={}, resolved_request={}
        ),
        "action_executions",
    )
    _add(
        ActionSecret(tenant_id=tenant.id, provider="crm", key_name="api_key", secret_value="shh"),
        "action_secrets",
    )
    _add(TenantAutomationConfig(tenant_id=tenant.id), "tenant_automation_configs")
    flow = Flow(tenant_id=tenant.id, name="Welcome", trigger_type=FlowTriggerType.MANUAL)
    _add(flow, "flows")
    _add(
        TenantChannelPost(
            tenant_id=tenant.id, title="Update", excerpt="short", content="long form content"
        ),
        "tenant_channel_posts",
    )
    customer = CustomerUser(
        full_name="Test Customer",
        email=f"customer-{uuid4().hex[:8]}@example.com",
        hashed_password="$2b$12$placeholder_hash_not_real",
    )
    db_session.add(customer)
    db_session.flush()
    _add(
        CustomerBusinessFollow(customer_user_id=customer.id, tenant_id=tenant.id),
        "customer_business_follows",
    )
    _add(
        NotificationLog(
            tenant_id=tenant.id,
            event=NotifEvent.MEETING_BOOKED,
            channel=NotifChannel.EMAIL,
            entity_type="lead",
            entity_id=uuid4(),
            recipient="notify@example.com",
        ),
        "notification_logs",
    )
    _add(
        DailyVisit(
            visit_date=utcnow_aware().date(),
            tenant_id=tenant.id,
            source="chat_widget",
            visitor_key="anon-1",
        ),
        "daily_visits",
    )
    _add(
        PromotionalGrant(
            tenant_id=tenant.id,
            campaign_code="LAUNCH",
            granted_tier="professional",
            duration_days=30,
            grant_starts_at=utcnow_aware(),
            grant_ends_at=utcnow_aware(),
            previous_tier="trial",
            previous_status="trialing",
            status="expired",
            source="manual",
            reason="test",
        ),
        "promotional_grants",
    )
    _add(TenantAIConfig(tenant_id=tenant.id), "tenant_ai_configs")
    _add(TenantCapabilities(tenant_id=tenant.id), "tenant_capabilities")
    _add(
        EmailNotification(
            tenant_id=tenant.id,
            notification_type="trial_expiring",
            recipient_email="owner@example.com",
            subject="hi",
        ),
        "email_notifications",
    )
    _add(
        PlatformFeedback(
            tenant_id=tenant.id,
            category="general",
            title="t",
            description="d",
            submitted_by=agent.id,
        ),
        "platform_feedback",
    )
    _add(
        Ticket(
            tenant_id=tenant.id,
            session_id=(
                db_session.execute(
                    select(ConversationSession.id).where(ConversationSession.tenant_id == tenant.id)
                )
                .scalars()
                .first()
            ),
            ticket_number=f"TKT-{uuid4().hex[:10]}",
            summary="test",
            assigned_to=agent.id,
        ),
        "tickets",
    )
    session_id = (
        db_session.execute(
            select(ConversationSession.id).where(ConversationSession.tenant_id == tenant.id)
        )
        .scalars()
        .first()
    )
    _add(_make_attachment(tenant.id, session_id), "message_attachments")

    return seeded


def test_purge_tenant_completeness_guard_zero_pii_bearing_rows_survive(db_session):
    """The test that would have caught the original miss: seeds a real row
    in every single tenant-scoped table (schema-discovered, not read off
    the service's own delete list), purges the tenant, and asserts zero
    surviving rows per table except the explicit allowlist. A future
    tenant-scoped table that forgets to get added to the service's delete
    list fails THIS test with the offending table's name, instead of
    silently leaking PII.
    """
    tenant = _make_tenant(db_session)
    admin = _make_user(db_session, tenant, role=UserRole.TENANT_ADMIN)
    agent = _make_user(db_session, tenant, role=UserRole.SUPPORT_AGENT)

    tables = _tenant_scoped_tables()
    table_names = {t.name for t in tables}
    seeded = _seed_every_tenant_scoped_table(db_session, tenant, agent)

    # Every discovered table except the allowlist must have an actual row
    # for this tenant — otherwise the assertion loop below would pass
    # vacuously on any table nobody seeded, defeating the point of the guard.
    unseeded = table_names - seeded - _PURGE_TENANT_ALLOWLIST
    assert not unseeded, f"completeness guard has no fixture for: {sorted(unseeded)}"
    assert len(tables) >= 35  # sanity: discovery found the real schema

    service = AccountPurgeService(db_session)
    service.purge_tenant(tenant.id)

    for table in tables:
        if table.name in _PURGE_TENANT_ALLOWLIST:
            continue
        count = db_session.execute(
            select(func.count()).select_from(table).where(table.c.tenant_id == tenant.id)
        ).scalar_one()
        assert count == 0, f"{table.name} still has rows for the purged tenant"

    # The one allowlisted table that DOES keep rows (as shells, not
    # deleted) must have zero rows left un-purged for this tenant.
    db_session.refresh(admin)
    db_session.refresh(agent)
    remaining_active = db_session.execute(
        select(func.count())
        .select_from(User)
        .where(User.tenant_id == tenant.id, User.deletion_status != DeletionStatus.PURGED)
    ).scalar_one()
    assert remaining_active == 0
    # whatsapp_inbound_messages has no tenant_id (so the schema-discovered loop skips it)
    assert (
        db_session.execute(
            select(func.count())
            .select_from(WhatsAppInboundMessage)
            .where(WhatsAppInboundMessage.provider_message_id == "PURGE-GUARD")
        ).scalar_one()
        == 0
    )
