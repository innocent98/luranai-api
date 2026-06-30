"""NotificationService — synchronous dispatch spine (Stage 3 Slice 5a).

Writes NotificationLog rows idempotently and returns the ARQ jobs to enqueue;
the caller enqueues in its own (sync/async) context. SMTP happens in the worker.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models.lead import Lead
from app.db.models.notification_log import NotificationLog, NotifChannel, NotifEvent, NotifStatus
from app.db.models.sales_agent_config import TenantSalesAgentConfig
from app.db.models.user import User, UserRole

# (event → does it notify the visitor?)
_VISITOR_EVENTS = {NotifEvent.MEETING_BOOKED}


class NotificationService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def _rep_recipient(self, tenant_id: uuid.UUID, config: TenantSalesAgentConfig | None) -> str | None:
        if config is not None and config.notification_email:
            return config.notification_email
        admin = self.db.execute(
            select(User).where(User.tenant_id == tenant_id, User.role == UserRole.TENANT_ADMIN)
        ).scalars().first()
        return admin.email if admin is not None else None

    def _visitor_recipient(self, entity_id: uuid.UUID) -> str | None:
        # meeting_booked: entity is a meeting; the lead's email is the visitor
        from app.db.models.meeting import Meeting
        m = self.db.get(Meeting, entity_id)
        if m is None:
            return None
        lead = self.db.get(Lead, m.lead_id)
        return lead.email if lead is not None and lead.email else None

    def _try_log(self, *, tenant_id, event, channel, entity_type, entity_id, recipient) -> uuid.UUID | None:
        """INSERT a pending log; return its id, or None if it already exists (idempotent).

        Uses a nested SAVEPOINT so only the duplicate INSERT is rolled back — the
        caller's (outer) session state is left intact regardless of outcome.
        """
        nested = self.db.begin_nested()
        log = NotificationLog(
            tenant_id=tenant_id, event=event, channel=channel,
            entity_type=entity_type, entity_id=entity_id, recipient=recipient,
            status=NotifStatus.PENDING)
        self.db.add(log)
        try:
            self.db.flush()
        except IntegrityError:
            nested.rollback()
            return None
        else:
            nested.commit()
        return log.id

    def dispatch(self, tenant_id, event, *, entity_type, entity_id) -> list[tuple[uuid.UUID, str]]:
        config = self.db.execute(
            select(TenantSalesAgentConfig).where(TenantSalesAgentConfig.tenant_id == tenant_id)
        ).scalar_one_or_none()
        channels = (config.notify_channels if config is not None else {}) or {}
        jobs: list[tuple[uuid.UUID, str]] = []

        if channels.get("email"):
            rep = self._rep_recipient(tenant_id, config)
            if rep:
                log_id = self._try_log(tenant_id=tenant_id, event=event, channel=NotifChannel.EMAIL,
                                       entity_type=entity_type, entity_id=entity_id, recipient=rep)
                if log_id is not None:
                    jobs.append((log_id, "send_rep_alert"))
            if event in _VISITOR_EVENTS:
                visitor = self._visitor_recipient(entity_id)
                if visitor:
                    log_id = self._try_log(tenant_id=tenant_id, event=event, channel=NotifChannel.EMAIL,
                                           entity_type=entity_type, entity_id=entity_id, recipient=visitor)
                    if log_id is not None:
                        jobs.append((log_id, "send_visitor_meeting_confirmation"))

        # Logs are committed BEFORE the caller enqueues into Redis. If Redis is
        # down, the log row persists as PENDING with no queued job. A stale-PENDING
        # sweeper (requeue on startup / periodic scan) is a later-slice follow-up.
        self.db.commit()
        return jobs
