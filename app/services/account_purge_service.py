"""AccountPurgeService — the irreversible purge: no retained subset, no
legal-hold carve-out. After the grace period, purge means purge.

This is the destructive half of account deletion (see
``app/services/account_deletion_service.py`` for the reversible
request/verify/cancel lifecycle that leads up to it). Nothing here is
undoable. Callers (the Task 4 endpoint / Task 5 worker) must only invoke
this once a ``DeletionRequest`` has actually reached the end of its grace
period.

============================================================================
Design choices worth knowing before extending this file
============================================================================

* **``purge_user`` deletes what is purely the user's own (RefreshToken,
  AgentDevice rows), and scrubs every PII column on the ``User`` row to a
  generic, non-identifying value. No PII is retained anywhere.**

* **The ``User`` row is kept as an empty shell, not hard-deleted — a
  deliberate call, not an oversight.** ``deletion_requests.user_id`` is
  ``NOT NULL`` with ``ondelete="CASCADE"`` (see app/db/models/
  deletion_request.py). Hard-deleting the ``User`` row would cascade-delete
  every ``DeletionRequest`` row for that user — destroying the very
  "purge happened" audit trail this service is required to preserve.
  Making that column nullable would need an Alembic migration, which is
  outside this task's file scope (service + tests only). Keeping a fully
  PII-free shell row satisfies "no PII remains anywhere for that user"
  without a schema change, and lets the existing `DeletionRequest.user_id`
  FK keep resolving. Same reasoning applies to ``purge_tenant`` and the
  ``Tenant`` row (``deletion_requests.tenant_id`` is also ``NOT NULL`` +
  ``CASCADE``). Flagged for follow-up: if a true hard-delete of the
  User/Tenant row is ever required, migrate
  ``deletion_requests.user_id``/``tenant_id`` to nullable + ``SET NULL``
  first.

* **Tenant-owned records that merely reference the user are de-identified,
  not deleted.** A support ticket a since-deleted agent handled, or a chat
  session they joined, is the *tenant's* (and its customers') data, not the
  departing agent's. Every inbound FK to ``users.id`` was enumerated via
  ``grep -rn 'ForeignKey("users.id"' app/db/models/*.py`` (15 occurrences,
  all handled below — see ``_deidentify_user_references``): the reference
  column is set to ``NULL`` (or, for the one place that also denormalizes
  the user's raw email/name as platform marketing data, redacted) while the
  owning row survives untouched.

* **Not every leak is FK-shaped — fix round 2.** Two tables denormalize
  the user's email *by value* into a ``tenant_id``-scoped row with no
  ``users.id`` column at all (``email_notifications.recipient_email``,
  ``notification_logs.recipient``), so the FK grep above structurally
  cannot find them. ``_redact_denormalized_pii_by_value`` matches these by
  the user's original email string instead — which means it MUST run
  before ``_anonymize_user`` overwrites ``user.email`` (same ordering trap
  as the ``audit_logs.ip_address`` fix in round 1: the match key has to be
  captured/used while it's still the real value).

* **``purge_tenant`` hard-deletes every tenant-scoped table — no retained
  billing/audit subset.** ``payment_transactions``, ``subscription_
  history``, ``promotional_grants``, ``audit_logs`` and
  ``analytics_metrics`` are deleted along with everything else. The one
  exception is ``deletion_requests`` itself (see above) — the PII-free
  audit trail that proves the purge ran.

* **``purge_tenant`` commits once, at the end.** Every delete/de-identify
  step runs against the same open transaction; a failure partway through
  rolls back the whole cascade instead of leaving the tenant half-purged.
  ``purge_user`` commits on its own — it's a self-contained, independently
  idempotent operation.

============================================================================
What gets deleted vs. de-identified — auditable field list
============================================================================
``User`` columns scrubbed by ``purge_user`` (irreversibly replaced, see
``_anonymize_user``):
    email            -> deterministic per-user tombstone address (never
                         matches a real signup; frees the original address
                         for reuse by someone else)
    email_hash       -> NOT set directly — recomputed automatically from the
                         tombstone email by the existing
                         ``_populate_user_email_hash`` listener in
                         app/db/models/user.py, which is what actually frees
                         the original email's hash for reuse.
    full_name        -> generic "Deleted user" label
    hashed_password  -> random, discarded bcrypt hash (defence in depth —
                         login is already blocked by is_active=False +
                         deletion_status=PURGED before password is checked)
    is_active        -> False
    deletion_status  -> DeletionStatus.PURGED
Not touched (structural / not PII): id, tenant_id, role, created_at,
availability_status, must_change_password, last_login_at.

Deleted outright (purely the user's own artifacts, not tenant data):
    RefreshToken rows (user_id, user_type="tenant_user") — session tokens
    AgentDevice rows  (user_id)                          — FCM push tokens

De-identified in place (tenant/platform-owned records that merely
reference the user — the record survives, the attribution doesn't), one
entry per inbound FK found by the grep above:
    tickets.assigned_to                              -> NULL
    conversation_sessions.agent_id                   -> NULL
    flows.created_by                                 -> NULL
    tenant_actions.created_by / updated_by            -> NULL
    tenant_automation_configs.updated_by              -> NULL
    action_executions.approved_by                     -> NULL
    tenant_channel_posts.created_by_user_id /
        published_by_user_id                          -> NULL
    platform_feedback.submitted_by                    -> NULL
    processing_jobs.created_by                        -> NULL
    audit_logs.user_id                                -> NULL, plus
        .ip_address / .user_agent -> NULL in the SAME statement (GDPR
        personal data denormalized on the row itself — fixed in fix round
        1; nulling them in a second, separately-keyed UPDATE after user_id
        is already NULL would silently match zero rows)
    platform_email_campaign_recipients.user_id        -> NULL, plus
        .recipient_email -> redacted placeholder and .recipient_name ->
        NULL (the only inbound-FK table that also denormalizes the user's
        raw email/name as platform marketing data)
Left alone: deletion_requests.user_id — the audit trail (see above); no PII
lives on that row to begin with (token hashes only).

``Message`` (chat content) has no per-message author column in this schema
— human-agent attribution lives entirely on
``ConversationSession.agent_id``, which is de-identified above. There is
nothing else to scrub for message authorship.

Not an inbound FK, but scrubbed alongside the sweep above (fixed in fix
round 1 — was flagged as a known gap in the original submission, now
closed): ``subscription_history.changed_by_id`` is a bare UUID column with
no declared FK to ``users.id``, but ``changed_by_name`` denormalizes a
free-text name. ``_deidentify_user_references`` nulls
``changed_by_name`` wherever ``changed_by_id`` matches the purged user.
``changed_by_id`` itself is left as a bare UUID (not PII on its own, no FK
to null) — not a concern for ``purge_tenant``, which deletes
``subscription_history`` entirely.

**Reviewed and found NOT to apply** (fix round 1 checked this explicitly
rather than guessing): ``PlatformFeedback`` has no ``name``/``email``/
``company`` columns — those free-text contact fields exist only on the
separate, non-tenant-scoped ``PublicFeedback`` model (anonymous
landing-page feedback, no ``submitted_by``/user FK at all, out of this
service's remit entirely). ``PlatformFeedback.submitted_by`` nulling above
is the complete fix for that table on the single-user path.

Not FK-shaped at all — denormalized by value, keyed by the user's email
string (fixed in fix round 2, see ``_redact_denormalized_pii_by_value``):
    email_notifications.recipient_email  -> redacted placeholder, plus
        .template_data -> NULL (confirmed, not hypothetical: at least one
        flow sets template_data["admin_name"] = admin.full_name — see
        email_notification_service.py:326 — so the whole JSON blob is
        nulled rather than picked apart key-by-key)
    notification_logs.recipient          -> redacted placeholder
        (.title/.body checked and found to describe the lead/meeting/
        quote being notified about, never the recipient's own name — left
        untouched, not guessed at)
Both matched on `tenant_id` + the user's ORIGINAL email (captured before
``_anonymize_user`` overwrites it — these tables have no ``users.id``
column to match on instead). On ``purge_tenant``'s per-member loop this is
a harmless no-op, since both tables are already hard-deleted for the whole
tenant earlier in that method — kept for defence in depth.

============================================================================
``purge_tenant`` — tables hard-deleted (tenant-scoped; no retained subset)
============================================================================
conversation_sessions (cascades messages + tickets via their own
    ondelete=CASCADE FKs), session_ratings, leads, meetings, quotes,
    knowledge_sources (cascades knowledge_chunks), tenant_channel_posts,
    customer_business_follows, visitors (cascades visitor_sessions),
    notification_logs, daily_visits, agent_devices, api_keys,
    business_profiles, tenant_sales_agent_config,
    tenant_automation_configs, tenant_actions (cascades action_executions),
    action_secrets, flows (cascades flow_executions), audit_reports
    (cascades audit_entries), processing_jobs, payment_transactions,
    subscription_history, promotional_grants, audit_logs (tenant-scoped
    rows only — a NULL tenant_id row is a platform-level action, not this
    tenant's), analytics_metrics, tenant_ai_configs, tenant_capabilities,
    email_notifications (recipient_email — added in fix round 1, was
    missing from the original delete list), platform_feedback (added in
    fix round 1, same root cause).

This list is now guarded by a completeness test
(``test_purge_tenant_completeness_guard_...`` in the test file) that
reflects every model with a declared ``tenant_id`` FK to ``tenants.id``
straight off ``Base.metadata`` and asserts zero surviving rows per table
except the explicit allowlist below — so a future tenant-scoped table
that forgets to get added here fails the test instead of silently
leaking. This is the fix for the original miss: because the ``Tenant``
row is kept as a shell rather than hard-deleted, ``ondelete=CASCADE``
never fires, and this explicit delete list is the ONLY thing that removes
tenant data — it has to be exhaustive, and now it's mechanically checked.

Left alone: ``deletion_requests`` (the audit trail — see above);
``platform_email_campaigns`` / ``platform_email_campaign_recipients``
(Luran's own outbound record, deliberately ``ondelete="SET NULL"`` rather
than CASCADE on tenant_id — not this tenant's data to delete; members'
rows are still de-identified via ``purge_user``, see above).

``Tenant`` row: kept as a PII-free shell for the same FK reason as
``User`` (see "Design choices"). Identity/contact fields are scrubbed
(name, display_name, description*, support_*, public_slug,
widget_logo_url, api_key_hash); status flipped to INACTIVE and
unlisted. Operational/non-identifying fields (subscription_tier,
category, widget colors, etc.) are left as-is — deliberately not the
target of a PII purge.
============================================================================
"""

import logging
import uuid
from uuid import UUID

from passlib.context import CryptContext
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.db.models.agent_device import AgentDevice
from app.db.models.api_key import APIKey
from app.db.models.audit import AnalyticsMetric, AuditEntry, AuditLog, AuditReport
from app.db.models.automation import (
    ActionExecution,
    ActionSecret,
    TenantAction,
    TenantAutomationConfig,
)
from app.db.models.business_profile import BusinessProfile
from app.db.models.conversation import ConversationSession, MessageAttachment, Ticket
from app.db.models.customer_engagement import CustomerBusinessFollow, TenantChannelPost
from app.db.models.deletion_request import DeletionRequest
from app.db.models.email_notification import EmailNotification
from app.db.models.flow import Flow, FlowExecution
from app.db.models.job import ProcessingJob
from app.db.models.knowledge import KnowledgeChunk, KnowledgeSource
from app.db.models.lead import Lead
from app.db.models.meeting import Meeting
from app.db.models.notification_log import NotificationLog
from app.db.models.payment import PaymentTransaction
from app.db.models.platform_email_campaign import PlatformEmailCampaignRecipient
from app.db.models.promotional_grant import PromotionalGrant
from app.db.models.quote import Quote
from app.db.models.refresh_token import RefreshToken
from app.db.models.review import PlatformFeedback, SessionRating
from app.db.models.sales_agent_config import TenantSalesAgentConfig
from app.db.models.subscription import SubscriptionHistory
from app.db.models.tenant import Tenant, TenantAIConfig, TenantCapabilities, TenantStatus
from app.db.models.user import DeletionStatus, User
from app.db.models.visit_analytics import DailyVisit
from app.db.models.visitor import Visitor, VisitorSession
from app.utils.cloudinary_client import cloudinary_manager

_TOMBSTONE_FULL_NAME = "Deleted user"
_TOMBSTONE_TENANT_NAME_PREFIX = "deleted-tenant-"
_TOMBSTONE_DISPLAY_NAME = "Deleted tenant"

# Bcrypt, matching the hashing scheme used everywhere else a password/API
# key is stored (see conftest.py fixtures / app/core/security.py).
_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

logger = logging.getLogger(__name__)


class AccountPurgeService:
    """Irreversible PII purge + tenant cascade. See module docstring."""

    def __init__(self, db: Session) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def purge_user(self, user_id: UUID) -> None:
        """Purge a single user and commit.

        Idempotent: a missing user, or one already ``PURGED``, is a
        no-op — safe to call more than once (e.g. a retried worker task).
        """
        user = self.db.get(User, user_id)
        if user is None or user.deletion_status == DeletionStatus.PURGED:
            return

        self._deidentify_user_references(user.id)
        self._redact_denormalized_pii_by_value(user)
        self._anonymize_user(user)
        self.db.commit()

    def purge_tenant(self, tenant_id: UUID) -> None:
        """Cascade-purge a tenant: hard-delete every tenant-scoped table
        (see the module docstring for the full list — there is no retained
        subset) and purge every member user. Idempotent — safe to call
        more than once; already-deleted rows and already-purged members
        are no-ops. Commits once, at the end, so a failure partway through
        rolls back the whole cascade instead of leaving the tenant
        half-purged.
        """
        tenant = self.db.get(Tenant, tenant_id)
        if tenant is None:
            return

        # --- tenant-scoped data: children before parents. Most of these
        # FKs also cascade at the DB level (ondelete="CASCADE"), but
        # deleting explicitly keeps this auditable and still correct even
        # if a future migration ever drops that constraint. ---
        self.db.execute(delete(SessionRating).where(SessionRating.tenant_id == tenant_id))

        self.db.execute(delete(ActionExecution).where(ActionExecution.tenant_id == tenant_id))
        self.db.execute(delete(ActionSecret).where(ActionSecret.tenant_id == tenant_id))
        self.db.execute(
            delete(TenantAutomationConfig).where(TenantAutomationConfig.tenant_id == tenant_id)
        )
        self.db.execute(delete(TenantAction).where(TenantAction.tenant_id == tenant_id))

        flow_ids = (
            self.db.execute(select(Flow.id).where(Flow.tenant_id == tenant_id)).scalars().all()
        )
        if flow_ids:
            self.db.execute(delete(FlowExecution).where(FlowExecution.flow_id.in_(flow_ids)))
        self.db.execute(delete(Flow).where(Flow.tenant_id == tenant_id))

        self.db.execute(delete(Quote).where(Quote.tenant_id == tenant_id))
        self.db.execute(delete(Meeting).where(Meeting.tenant_id == tenant_id))
        self.db.execute(delete(Lead).where(Lead.tenant_id == tenant_id))

        self.db.execute(delete(KnowledgeChunk).where(KnowledgeChunk.tenant_id == tenant_id))
        self.db.execute(delete(KnowledgeSource).where(KnowledgeSource.tenant_id == tenant_id))

        self.db.execute(delete(TenantChannelPost).where(TenantChannelPost.tenant_id == tenant_id))
        self.db.execute(
            delete(CustomerBusinessFollow).where(CustomerBusinessFollow.tenant_id == tenant_id)
        )

        self.db.execute(delete(VisitorSession).where(VisitorSession.tenant_id == tenant_id))
        self.db.execute(delete(Visitor).where(Visitor.tenant_id == tenant_id))

        self.db.execute(delete(NotificationLog).where(NotificationLog.tenant_id == tenant_id))
        self.db.execute(delete(DailyVisit).where(DailyVisit.tenant_id == tenant_id))

        self.db.execute(delete(AgentDevice).where(AgentDevice.tenant_id == tenant_id))
        self.db.execute(delete(APIKey).where(APIKey.tenant_id == tenant_id))
        self.db.execute(delete(BusinessProfile).where(BusinessProfile.tenant_id == tenant_id))
        self.db.execute(
            delete(TenantSalesAgentConfig).where(TenantSalesAgentConfig.tenant_id == tenant_id)
        )

        self.db.execute(delete(AuditEntry).where(AuditEntry.tenant_id == tenant_id))
        self.db.execute(delete(AuditReport).where(AuditReport.tenant_id == tenant_id))
        self.db.execute(delete(ProcessingJob).where(ProcessingJob.tenant_id == tenant_id))
        self.db.execute(delete(EmailNotification).where(EmailNotification.tenant_id == tenant_id))
        self.db.execute(delete(PlatformFeedback).where(PlatformFeedback.tenant_id == tenant_id))

        # Billing/subscription/audit — no retained subset any more.
        self.db.execute(delete(PaymentTransaction).where(PaymentTransaction.tenant_id == tenant_id))
        self.db.execute(
            delete(SubscriptionHistory).where(SubscriptionHistory.tenant_id == tenant_id)
        )
        self.db.execute(delete(PromotionalGrant).where(PromotionalGrant.tenant_id == tenant_id))
        self.db.execute(delete(AuditLog).where(AuditLog.tenant_id == tenant_id))
        self.db.execute(delete(AnalyticsMetric).where(AnalyticsMetric.tenant_id == tenant_id))

        self.db.execute(delete(TenantAIConfig).where(TenantAIConfig.tenant_id == tenant_id))
        self.db.execute(delete(TenantCapabilities).where(TenantCapabilities.tenant_id == tenant_id))

        # Chat image attachments: remove the Cloudinary assets first, then the
        # rows. A Cloudinary failure must NEVER abort the purge (a half-purged
        # tenant is worse than an orphaned CDN asset), so each destroy is
        # best-effort; `destroy` already treats "not found" as success.
        attachment_public_ids = (
            self.db.execute(
                select(MessageAttachment.cloudinary_public_id).where(
                    MessageAttachment.tenant_id == tenant_id
                )
            )
            .scalars()
            .all()
        )
        for public_id in attachment_public_ids:
            try:
                ok, err = cloudinary_manager.destroy(public_id)
                if not ok:
                    logger.warning(
                        "Cloudinary destroy failed during tenant purge (continuing): %s", err
                    )
            except Exception:
                logger.exception("Cloudinary destroy raised during tenant purge (continuing)")
        self.db.execute(delete(MessageAttachment).where(MessageAttachment.tenant_id == tenant_id))

        # Sessions last among the "chat" family — Message.session_id and
        # Ticket.session_id both declare ondelete="CASCADE" at the DB level,
        # so this one delete also removes their rows.
        self.db.execute(
            delete(ConversationSession).where(ConversationSession.tenant_id == tenant_id)
        )

        # --- tenant row itself: kept as a PII-free shell (see module
        # docstring — deletion_requests.tenant_id is NOT NULL + CASCADE). ---
        self._anonymize_tenant(tenant)

        # --- member users: purge every member via the same path
        # purge_user uses (idempotent — already-purged members skipped).
        # Any FK references from tables already deleted above are moot;
        # de-identifying them is a harmless no-op update. ---
        members = self.db.execute(select(User).where(User.tenant_id == tenant_id)).scalars().all()
        for member in members:
            if member.deletion_status != DeletionStatus.PURGED:
                self._deidentify_user_references(member.id)
                # email_notifications/notification_logs for this tenant were
                # already hard-deleted above, so this is a harmless no-op
                # here — kept for defence in depth / consistency with the
                # purge_user path, in case the delete order above ever
                # changes.
                self._redact_denormalized_pii_by_value(member)
                self._anonymize_user(member)

        self.db.commit()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _anonymize_user(self, user: User) -> None:
        """No-commit PII scrub of the ``User`` row itself. Caller commits."""
        user.email = f"deleted-user-{user.id}@purged.invalid"
        # email_hash is NOT set here — the User model's before_update
        # listener (app/db/models/user.py::_populate_user_email_hash)
        # recomputes it from the line above on every flush, which is what
        # actually frees the original email's hash for reuse.
        user.full_name = _TOMBSTONE_FULL_NAME
        user.hashed_password = _pwd_context.hash(uuid.uuid4().hex)
        user.is_active = False
        user.deletion_status = DeletionStatus.PURGED

        self.db.execute(
            delete(RefreshToken).where(
                RefreshToken.user_id == user.id, RefreshToken.user_type == "tenant_user"
            )
        )
        self.db.execute(delete(AgentDevice).where(AgentDevice.user_id == user.id))

        # Mark the audit trail. No PII lives on DeletionRequest to begin
        # with (token hashes only) — this just finalizes its status.
        open_requests = (
            self.db.execute(
                select(DeletionRequest).where(
                    DeletionRequest.user_id == user.id,
                    DeletionRequest.status != DeletionStatus.PURGED,
                )
            )
            .scalars()
            .all()
        )
        for request in open_requests:
            request.status = DeletionStatus.PURGED

    def _deidentify_user_references(self, user_id: UUID) -> None:
        """NULL out every inbound FK from a tenant/platform-owned record to
        this user (found via ``grep -rn 'ForeignKey("users.id"'
        app/db/models/*.py`` — see the module docstring for the full,
        one-per-FK list). The owning record survives; only the "who"
        is removed. No-commit — caller commits.
        """
        self.db.execute(
            Ticket.__table__.update().where(Ticket.assigned_to == user_id).values(assigned_to=None)
        )
        self.db.execute(
            ConversationSession.__table__.update()
            .where(ConversationSession.agent_id == user_id)
            .values(agent_id=None)
        )
        self.db.execute(
            Flow.__table__.update().where(Flow.created_by == user_id).values(created_by=None)
        )
        self.db.execute(
            TenantAction.__table__.update()
            .where(TenantAction.created_by == user_id)
            .values(created_by=None)
        )
        self.db.execute(
            TenantAction.__table__.update()
            .where(TenantAction.updated_by == user_id)
            .values(updated_by=None)
        )
        self.db.execute(
            TenantAutomationConfig.__table__.update()
            .where(TenantAutomationConfig.updated_by == user_id)
            .values(updated_by=None)
        )
        self.db.execute(
            ActionExecution.__table__.update()
            .where(ActionExecution.approved_by == user_id)
            .values(approved_by=None)
        )
        self.db.execute(
            TenantChannelPost.__table__.update()
            .where(TenantChannelPost.created_by_user_id == user_id)
            .values(created_by_user_id=None)
        )
        self.db.execute(
            TenantChannelPost.__table__.update()
            .where(TenantChannelPost.published_by_user_id == user_id)
            .values(published_by_user_id=None)
        )
        self.db.execute(
            PlatformFeedback.__table__.update()
            .where(PlatformFeedback.submitted_by == user_id)
            .values(submitted_by=None)
        )
        self.db.execute(
            ProcessingJob.__table__.update()
            .where(ProcessingJob.created_by == user_id)
            .values(created_by=None)
        )
        # ip_address/user_agent are personal data too (GDPR) — must go in
        # the SAME statement as nulling user_id: a second UPDATE keyed on
        # AuditLog.user_id == user_id would match zero rows once the first
        # one has already cleared that column.
        self.db.execute(
            AuditLog.__table__.update()
            .where(AuditLog.user_id == user_id)
            .values(user_id=None, ip_address=None, user_agent=None)
        )

        # subscription_history.changed_by_id is a bare UUID column with no
        # declared FK to users.id (so it's outside the grep-driven FK sweep
        # above), but changed_by_name denormalizes a free-text name —
        # scrub it explicitly. Not a concern for purge_tenant, which
        # deletes subscription_history entirely.
        self.db.execute(
            SubscriptionHistory.__table__.update()
            .where(SubscriptionHistory.changed_by_id == user_id)
            .values(changed_by_name=None)
        )

        # The one inbound-FK table that also denormalizes raw PII —
        # redact the email/name alongside nulling the FK.
        recipients = (
            self.db.execute(
                select(PlatformEmailCampaignRecipient).where(
                    PlatformEmailCampaignRecipient.user_id == user_id
                )
            )
            .scalars()
            .all()
        )
        for recipient in recipients:
            recipient.user_id = None
            recipient.recipient_email = f"redacted-{recipient.id}@purged.invalid"
            recipient.recipient_name = None

    def _redact_denormalized_pii_by_value(self, user: User) -> None:
        """Scrub the user's PII out of tenant-owned tables that denormalize
        it BY VALUE rather than by FK to ``users.id`` — fix round 2, the
        same class of gap as ``changed_by_name``/``ip_address`` in
        ``_anonymize_user``, just on tables the FK sweep in
        ``_deidentify_user_references`` structurally cannot reach (no
        ``users.id`` column at all; only ``tenant_id`` + a raw copy of the
        address).

        **Must be called with the user's ORIGINAL ``.email`` still intact**
        — i.e. before ``_anonymize_user`` overwrites it — since the match
        key is the email string itself, not an id. Both call sites
        (``purge_user``, ``purge_tenant``'s per-member loop) already call
        this before ``_anonymize_user``; do not reorder.

        Verified against the actual model columns (not assumed):
        ``email_notifications.recipient_email`` (``app/db/models/
        email_notification.py``) is set to a tenant admin's raw email by
        ``EmailNotificationService`` for trial/subscription reminders —
        confirmed by reading ``app/services/email_notification_service.py``,
        e.g. ``recipient_email=admin.email``. Its ``template_data`` JSON
        blob also denormalizes the recipient's name in at least one flow
        (``template_data["admin_name"] = admin.full_name``,
        ``email_notification_service.py:326``) — confirmed, not
        hypothetical, so it is nulled outright rather than picked apart
        key-by-key. ``notification_logs.recipient`` (``app/db/models/
        notification_log.py``) is the tenant rep's email, set by
        ``NotificationService._rep_recipient`` (``app/services/
        notification_service.py``). Its ``title``/``body`` were checked
        too (``NotificationService._render_in_app_text``) and found to
        describe the lead/meeting/quote being notified about, never the
        recipient's own name — left untouched, not guessed at.

        ``User`` has no ``phone`` column (checked — see the module
        docstring's PII field list), so there is nothing to match on
        beyond email.
        """
        original_email = user.email
        if not original_email:
            return

        self.db.execute(
            EmailNotification.__table__.update()
            .where(
                EmailNotification.tenant_id == user.tenant_id,
                EmailNotification.recipient_email == original_email,
            )
            .values(
                recipient_email=f"redacted-{user.id}@purged.invalid",
                template_data=None,
            )
        )
        self.db.execute(
            NotificationLog.__table__.update()
            .where(
                NotificationLog.tenant_id == user.tenant_id,
                NotificationLog.recipient == original_email,
            )
            .values(recipient=f"redacted-{user.id}@purged.invalid")
        )

    def _anonymize_tenant(self, tenant: Tenant) -> None:
        """No-commit PII/identity scrub of the ``Tenant`` row itself, kept
        as a shell for the same FK reason as ``User`` (see module
        docstring). Caller commits.
        """
        tenant.name = f"{_TOMBSTONE_TENANT_NAME_PREFIX}{tenant.id}"
        tenant.display_name = _TOMBSTONE_DISPLAY_NAME
        tenant.description = None
        tenant.long_description = None
        tenant.support_headline = None
        tenant.support_subheadline = None
        tenant.support_hours = None
        tenant.support_response_time = None
        tenant.support_footer_note = None
        tenant.public_slug = None
        tenant.widget_logo_url = None
        tenant.api_key_hash = _pwd_context.hash(uuid.uuid4().hex)
        tenant.is_publicly_listed = False
        tenant.is_featured = False
        tenant.status = TenantStatus.INACTIVE
