"""ARQ jobs that ring agents' phones for an escalation.

Three jobs, all idempotent:
  * ``alert_escalation``          — ring tier 1 (available agents) immediately.
  * ``alert_escalation_fallback`` — deferred; ring supervisors only if still
    unclaimed, tell tier 1 they missed it, and mark the ticket MISSED.
  * ``clear_agent_alert``         — silent data push that stops the ring on the
    devices that lost the race.

Idempotency comes from the ``uq_notification_dedupe`` unique index, claimed via
``AgentAlertService.claim_log`` before each send. The fallback needs no
cancellation: it simply re-reads state and no-ops when the ticket is claimed.
"""

import logging
import uuid

from sqlalchemy import select, update

from app.core.config import settings
from app.core.websocket import connection_manager
from app.db.models.conversation import (
    ConversationSession,
    Message,
    MessageRole,
    Ticket,
    TicketStatus,
)
from app.db.models.notification_log import NotifEvent
from app.db.models.user import User
from app.db.session import SessionLocal
from app.services import fcm_client
from app.services.agent_alert_service import AgentAlertService
from app.services.first_responder import MISSED_NOTICE
from app.services.notification_enqueue import enqueue_clear_alert_async

logger = logging.getLogger(__name__)

_RING_NOTIFICATION = {
    "title": "Customer needs you",
    "body": "A conversation was escalated to a human agent.",
}


def _load(db, ticket_id: uuid.UUID) -> tuple[Ticket | None, ConversationSession | None]:
    ticket = db.get(Ticket, ticket_id)
    if ticket is None:
        return None, None
    return ticket, db.get(ConversationSession, ticket.session_id)


async def _push_tier(
    svc: AgentAlertService,
    *,
    tenant_id: uuid.UUID,
    ticket_id: uuid.UUID,
    event: NotifEvent,
    recipients: list[User],
    payload: dict[str, str],
    visible: bool,
) -> int:
    """Push `payload` to every device of every recipient. Returns devices rung."""
    rung = 0
    for user in recipients:
        log_id = svc.claim_log(tenant_id, event, ticket_id, user.id)
        if log_id is None:
            continue  # already pushed for this (ticket, tier, user)
        devices = svc.devices_for([user.id], tenant_id)
        ok_any = False
        last_error: str | None = None
        for device in devices:
            result = await fcm_client.send(
                device.device_token,
                payload,
                platform=device.platform,
                notification=_RING_NOTIFICATION if visible else None,
            )
            if result.ok:
                ok_any = True
                rung += 1
            else:
                last_error = result.error
                if result.unregistered:
                    svc.prune_token(device.user_id, device.device_token)
        svc.mark_log(log_id, sent=ok_any, error=last_error)
    return rung


async def alert_escalation(ctx, ticket_id: str) -> None:
    """Ring every available agent of the tenant (broadcast tier)."""
    if not settings.AGENT_ALERT_ENABLED:
        logger.info("AGENT_ALERT_ENABLED is off — skipping escalation ring")
        return

    db = SessionLocal()
    try:
        tid = uuid.UUID(ticket_id)
        ticket, session = _load(db, tid)
        if ticket is None or session is None:
            logger.warning("alert_escalation: ticket %s no longer exists", ticket_id)
            return
        if ticket.assigned_to is not None:
            return  # already claimed before the job even ran

        svc = AgentAlertService(db)
        recipients = svc.broadcast_recipients(ticket.tenant_id)
        if not recipients:
            logger.info("No available agents with devices for tenant %s", ticket.tenant_id)
            return

        rung = await _push_tier(
            svc,
            tenant_id=ticket.tenant_id,
            ticket_id=tid,
            event=NotifEvent.ESCALATION_ALERT,
            recipients=recipients,
            payload=svc.build_alert_payload(ticket, session),
            visible=True,
        )
        logger.info("Escalation %s rang %s device(s)", ticket_id, rung)
    finally:
        db.close()


async def alert_escalation_fallback(ctx, ticket_id: str) -> None:
    """After the wait window: escalate to supervisors if still unclaimed."""
    if not settings.AGENT_ALERT_ENABLED:
        return

    db = SessionLocal()
    try:
        tid = uuid.UUID(ticket_id)
        ticket, session = _load(db, tid)
        if ticket is None or session is None:
            return
        if ticket.assigned_to is not None or ticket.status != TicketStatus.OPEN:
            return  # claimed or otherwise resolved — idempotent no-op

        svc = AgentAlertService(db)
        tier_one_ids = svc.rung_user_ids(tid)
        supervisors = svc.supervisor_recipients(ticket.tenant_id)

        # Re-read immediately before ringing supervisors: a claim can land in
        # the gap between the check above and here — that check ran before the
        # recipient lookups just above, which are themselves a few DB
        # round-trips removed from the actual push below.
        db.refresh(ticket, attribute_names=["assigned_to"])
        if ticket.assigned_to is not None:
            logger.info("Escalation %s claimed just before fallback ring — skipping", ticket_id)
            return

        if supervisors:
            await _push_tier(
                svc,
                tenant_id=ticket.tenant_id,
                ticket_id=tid,
                event=NotifEvent.ESCALATION_FALLBACK,
                recipients=supervisors,
                payload=svc.build_alert_payload(ticket, session),
                visible=True,
            )

        # A tier-1 recipient who is also a supervisor is being paged right now,
        # not "missing" anything — exclude them from the missed notice.
        supervisor_ids = {s.id for s in supervisors}
        missed_targets = [
            user
            for user in (db.get(User, uid) for uid in tier_one_ids)
            if user is not None and user.id not in supervisor_ids
        ]
        if missed_targets:
            await _push_tier(
                svc,
                tenant_id=ticket.tenant_id,
                ticket_id=tid,
                event=NotifEvent.ESCALATION_MISSED,
                recipients=missed_targets,
                payload=svc.build_missed_payload(tid, "your supervisor"),
                visible=False,
            )

        # Flip to MISSED only if the ticket is still genuinely unclaimed. A
        # conditional UPDATE, not a read-then-write: a claim landing between
        # the re-read above and here must not be overwritten by this status
        # write (a claim in progress right now must never show as MISSED).
        result = db.execute(
            update(Ticket)
            .where(
                Ticket.id == tid,
                Ticket.assigned_to.is_(None),
                Ticket.status == TicketStatus.OPEN,
            )
            .values(status=TicketStatus.MISSED)
        )
        db.commit()

        if result.rowcount == 1:
            await _post_missed_notice(db, ticket.session_id)

        if result.rowcount == 0 and supervisors:
            # The ticket was claimed mid-push (after the re-read above, before
            # this UPDATE) — the supervisors we just paged may still be
            # ringing on an already-handled ticket. This clear's job id is
            # suffixed so it can fire even though the claim endpoint's own
            # clear (a different suffix) may already have run.
            winner_id = db.execute(
                select(Ticket.assigned_to).where(Ticket.id == tid)
            ).scalar_one_or_none()
            if winner_id is not None:
                try:
                    await enqueue_clear_alert_async(tid, winner_id, source="fallback")
                except Exception:  # noqa: BLE001 — a missed clear must not fail this job
                    logger.exception(
                        "Failed to enqueue fallback clear for escalation %s", ticket_id
                    )

        logger.info("Escalation %s fell back to %s supervisor(s)", ticket_id, len(supervisors))
    finally:
        db.close()


async def _post_missed_notice(db, session_id: uuid.UUID) -> None:
    """Tell the customer the team will reply (spec section 4.6). Never raises."""
    try:
        msg = Message(
            session_id=session_id,
            role=MessageRole.ASSISTANT,
            content=MISSED_NOTICE,
            meta={"system_notice": "ticket_missed"},
        )
        db.add(msg)
        db.commit()
        db.refresh(msg)
        await connection_manager.fanout_event(
            session_id=session_id,
            message={
                "type": "ticket_missed",
                "message_id": str(msg.id),
                "content": MISSED_NOTICE,
                "timestamp": msg.created_at.isoformat(),
            },
            to_customer=True,
            to_agents=False,
        )
    except Exception:  # noqa: BLE001 — a notice failure must not break the fallback
        logger.exception("Failed to post missed notice for session %s", session_id)


async def clear_agent_alert(ctx, ticket_id: str, winner_user_id: str) -> None:
    """Stop the ring on every device except the winner's."""
    if not settings.AGENT_ALERT_ENABLED:
        return

    db = SessionLocal()
    try:
        tid = uuid.UUID(ticket_id)
        winner_id = uuid.UUID(winner_user_id)
        ticket = db.get(Ticket, tid)
        if ticket is None:
            return

        svc = AgentAlertService(db)
        winner = db.get(User, winner_id)
        payload = svc.build_clear_payload(tid, winner.full_name if winner else "another agent")

        loser_ids = [uid for uid in svc.rung_user_ids(tid) if uid != winner_id]
        for device in svc.devices_for(loser_ids, ticket.tenant_id):
            result = await fcm_client.send(
                device.device_token, payload, platform=device.platform, notification=None
            )
            if result.unregistered:
                svc.prune_token(device.user_id, device.device_token)
    finally:
        db.close()
