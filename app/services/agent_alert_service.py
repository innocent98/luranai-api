"""AgentAlertService — recipient tiers, push payloads and delivery bookkeeping.

Recipient resolution deliberately happens at *send* time (inside the ARQ jobs),
not at escalation time: the supervisor-fallback fires 90 seconds later and
availability may have changed in between.

`_PRIORITY_WIRE` and `customer_name` are defined here, once, as the single
source of truth. Task 6's REST escalation-list endpoint imports both rather
than redeclaring them — a drifting priority map would make the push payload
and the REST list disagree about the same ticket's priority.
"""

import logging
import uuid
from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models.agent_device import AgentDevice
from app.db.models.conversation import ConversationSession, Ticket, TicketPriority
from app.db.models.notification_log import (
    NotificationLog,
    NotifChannel,
    NotifEvent,
    NotifStatus,
)
from app.db.models.user import AvailabilityStatus, User, UserRole
from app.db.models.visitor import Visitor
from app.services.fcm_client import stringify_data
from app.utils.datetime import utcnow_naive

logger = logging.getLogger(__name__)

_PRIORITY_WIRE = {
    TicketPriority.URGENT: "critical",
    TicketPriority.HIGH: "important",
    TicketPriority.MEDIUM: "normal",
    TicketPriority.LOW: "normal",
}

_ENTITY_TYPE = "ticket"


def customer_name(db: Session, session: ConversationSession) -> str:
    """Resolve a display name for the visitor on a session, defaulting to "Customer"."""
    if session.visitor_id is None:
        return "Customer"
    visitor = db.get(Visitor, session.visitor_id)
    return (visitor.name if visitor and visitor.name else None) or "Customer"


class AgentAlertService:
    """Everything the escalation-push ARQ jobs need from the database."""

    def __init__(self, db: Session) -> None:
        self.db = db

    # ---- recipients ------------------------------------------------------

    def broadcast_recipients(self, tenant_id: uuid.UUID) -> list[User]:
        """Tier 1: active, *available* agents of this tenant that have a device."""
        stmt = (
            select(User)
            .join(AgentDevice, AgentDevice.user_id == User.id)
            .where(
                User.tenant_id == tenant_id,
                User.is_active.is_(True),
                User.role.in_([UserRole.TENANT_ADMIN, UserRole.SUPPORT_AGENT]),
                User.availability_status == AvailabilityStatus.AVAILABLE,
            )
            .distinct()
        )
        return list(self.db.execute(stmt).scalars().all())

    def supervisor_recipients(self, tenant_id: uuid.UUID) -> list[User]:
        """Fallback tier: tenant admins with a device, availability ignored."""
        stmt = (
            select(User)
            .join(AgentDevice, AgentDevice.user_id == User.id)
            .where(
                User.tenant_id == tenant_id,
                User.is_active.is_(True),
                User.role == UserRole.TENANT_ADMIN,
            )
            .distinct()
        )
        return list(self.db.execute(stmt).scalars().all())

    def devices_for(self, user_ids: list[uuid.UUID], tenant_id: uuid.UUID) -> list[AgentDevice]:
        """Devices for these users, scoped to `tenant_id` — never trust caller-supplied ids alone."""
        if not user_ids:
            return []
        stmt = select(AgentDevice).where(
            AgentDevice.user_id.in_(user_ids),
            AgentDevice.tenant_id == tenant_id,
        )
        return list(self.db.execute(stmt).scalars().all())

    # ---- payloads --------------------------------------------------------

    def build_alert_payload(self, ticket: Ticket, session: ConversationSession) -> dict[str, str]:
        """The ring payload — contract §4.1 `escalation_alert`."""
        started = session.escalated_at or ticket.created_at or utcnow_naive()
        if started.tzinfo is not None:
            started = started.replace(tzinfo=None)
        wait_minutes = max(0, int((utcnow_naive() - started) / timedelta(minutes=1)))
        return stringify_data(
            {
                "type": "escalation_alert",
                "escalation_id": str(ticket.id),
                "session_id": str(session.id),
                "priority": _PRIORITY_WIRE.get(ticket.priority, "normal"),
                "customer_name": customer_name(self.db, session),
                "wait_minutes": wait_minutes,
                "reason": session.escalation_reason or ticket.summary or "Human help requested",
            }
        )

    def build_clear_payload(self, ticket_id: uuid.UUID, handled_by: str) -> dict[str, str]:
        """Silent data-only payload — contract §4.1 `clear_alert`."""
        return stringify_data(
            {
                "type": "clear_alert",
                "escalation_id": str(ticket_id),
                "handled_by": handled_by,
            }
        )

    def build_missed_payload(self, ticket_id: uuid.UUID, handled_by: str) -> dict[str, str]:
        """Low-priority payload — contract §4.1 `missed_escalation`."""
        return stringify_data(
            {
                "type": "missed_escalation",
                "escalation_id": str(ticket_id),
                "handled_by": handled_by,
            }
        )

    # ---- delivery bookkeeping -------------------------------------------

    def claim_log(
        self,
        tenant_id: uuid.UUID,
        event: NotifEvent,
        ticket_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> uuid.UUID | None:
        """Reserve the right to push (tenant, event, push, ticket, user).

        Returns the log id, or None when a row already exists — the unique index
        `uq_notification_dedupe` is what makes the ARQ jobs safe to retry. The
        recipient is the *user id*, never the device token (tokens must not be
        written to logs).
        """
        nested = self.db.begin_nested()
        log = NotificationLog(
            tenant_id=tenant_id,
            event=event,
            channel=NotifChannel.PUSH,
            entity_type=_ENTITY_TYPE,
            entity_id=ticket_id,
            recipient=str(user_id),
            status=NotifStatus.PENDING,
        )
        self.db.add(log)
        try:
            self.db.flush()
        except IntegrityError:
            nested.rollback()
            return None
        else:
            nested.commit()
        self.db.commit()
        return log.id

    def mark_log(self, log_id: uuid.UUID, *, sent: bool, error: str | None = None) -> None:
        log = self.db.get(NotificationLog, log_id)
        if log is None:
            return
        log.status = NotifStatus.SENT if sent else NotifStatus.FAILED
        log.error = None if sent else (error or "")[:500]
        self.db.commit()

    def rung_user_ids(self, ticket_id: uuid.UUID) -> list[uuid.UUID]:
        """Every user who was pushed for this escalation, across both ringing tiers."""
        stmt = select(NotificationLog.recipient).where(
            NotificationLog.entity_id == ticket_id,
            NotificationLog.channel == NotifChannel.PUSH,
            NotificationLog.event.in_(
                [NotifEvent.ESCALATION_ALERT, NotifEvent.ESCALATION_FALLBACK]
            ),
        )
        out: list[uuid.UUID] = []
        for recipient in self.db.execute(stmt).scalars().all():
            try:
                out.append(uuid.UUID(recipient))
            except ValueError:
                continue
        return list(dict.fromkeys(out))

    def reset_ring(self, ticket_id: uuid.UUID) -> None:
        """Clear this ticket's push-channel notification log rows.

        Only ever called when re-escalating a MISSED, still-unassigned ticket
        (see `chat.py`'s `_get_or_create_escalation_ticket`) — a genuinely new
        ring episode for the same ticket, not a retry of the old one.
        `claim_log`'s dedupe is keyed on (tenant, event, channel, ticket,
        recipient) with no notion of "episode": without clearing the old
        episode's rows first, any agent already rung (and missed) the first
        time would silently be skipped by `claim_log` on the second ring, even
        though a brand new ARQ job successfully enqueues and runs. Skip this
        and the fix looks complete but rings the exact agents who most need
        the second chance to answer.
        """
        self.db.execute(
            delete(NotificationLog).where(
                NotificationLog.entity_type == _ENTITY_TYPE,
                NotificationLog.entity_id == ticket_id,
                NotificationLog.channel == NotifChannel.PUSH,
            )
        )
        self.db.commit()

    def prune_token(self, user_id: uuid.UUID, device_token: str) -> None:
        """Drop a device FCM has reported as UNREGISTERED.

        Scoped to `user_id` as well as the token: the unique constraint on
        `agent_devices` is `(user_id, device_token)`, not `device_token` alone,
        so the same token string can legitimately belong to more than one
        agent's row (e.g. a device handed over from one agent to another
        without the first ever unregistering). Deleting by token alone would
        let an UNREGISTERED report for one agent silently unregister another.
        """
        self.db.execute(
            delete(AgentDevice).where(
                AgentDevice.user_id == user_id,
                AgentDevice.device_token == device_token,
            )
        )
        self.db.commit()
        logger.info("Pruned unregistered agent device token")
