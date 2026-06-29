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

    def _try_log(self, *, tenant_id, event, channel, entity_type, entity_id, recipient,
                 status=NotifStatus.PENDING, title=None, body=None) -> uuid.UUID | None:
        """INSERT a log row; return its id, or None if it already exists (idempotent).

        Uses a nested SAVEPOINT so only the duplicate INSERT is rolled back — the
        caller's (outer) session state is left intact regardless of outcome.
        """
        nested = self.db.begin_nested()
        log = NotificationLog(
            tenant_id=tenant_id, event=event, channel=channel,
            entity_type=entity_type, entity_id=entity_id, recipient=recipient,
            status=status, title=title, body=body)
        self.db.add(log)
        try:
            self.db.flush()
        except IntegrityError:
            nested.rollback()
            return None
        else:
            nested.commit()
        return log.id

    def _render_in_app_text(self, event, entity_type, entity_id) -> tuple[str, str]:
        """Return (title, body) strings for an in-app notification row.

        Loads the relevant entity from the DB; if the entity is missing a safe
        generic string is returned so dispatch never crashes.
        """
        from app.db.models.meeting import Meeting
        from app.db.models.quote import Quote

        if event == NotifEvent.SALES_READY_LEAD:
            lead = self.db.get(Lead, entity_id)
            name = (lead.name if lead else None) or "A lead"
            company = (lead.company if lead else None) or "—"
            score = lead.lead_score if lead else "—"
            return "New sales-ready lead", f"{name} · {company} · score {score}"

        if event == NotifEvent.MEETING_BOOKED:
            m = self.db.get(Meeting, entity_id)
            lead = self.db.get(Lead, m.lead_id) if m else None
            who = (lead.name if lead else None) or (lead.company if lead else None) or "a prospect"
            when = m.scheduled_at.strftime("%a %d %b · %H:%M UTC") if m and m.scheduled_at else ""
            return "Demo booked", f"{who} · {when}".strip(" ·")

        # quote_generated (default)
        from app.sales.pricing import _currency_symbol
        q = self.db.get(Quote, entity_id)
        lead = self.db.get(Lead, q.lead_id) if q else None
        company = (lead.company if lead else None) or "a prospect"
        total = f"{_currency_symbol(q.currency)}{int(q.total or 0):,}" if q else ""
        return "New quote", f"{total} · {company}".strip(" ·")

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

        if channels.get("in_app"):
            title, body = self._render_in_app_text(event, entity_type, entity_id)
            self._try_log(tenant_id=tenant_id, event=event, channel=NotifChannel.IN_APP,
                          entity_type=entity_type, entity_id=entity_id, recipient="in_app",
                          status=NotifStatus.SENT, title=title, body=body)

        if channels.get("slack") and getattr(config, "slack_webhook_url", None):
            title, body = self._render_in_app_text(event, entity_type, entity_id)
            log_id = self._try_log(tenant_id=tenant_id, event=event, channel=NotifChannel.SLACK,
                                   entity_type=entity_type, entity_id=entity_id, recipient="slack",
                                   status=NotifStatus.PENDING, title=title, body=body)
            if log_id is not None:
                jobs.append((log_id, "send_slack_notification"))

        # Logs are committed BEFORE the caller enqueues into Redis. If Redis is
        # down, the log row persists as PENDING with no queued job. A stale-PENDING
        # sweeper (requeue on startup / periodic scan) is a later-slice follow-up.
        self.db.commit()
        return jobs
