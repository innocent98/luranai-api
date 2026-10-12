"""Daily chat-attachment retention (spec SR-D5, §4.5).

LINKED attachments past expires_at have their Cloudinary asset destroyed. The row is
kept as EXPIRED with url/poster_url NULL, so transcripts show a placeholder. An
attachment whose session has a live ticket (open / in_progress / missed) is deferred,
but never for more than ACTIVE_GRACE past expiry. One failing row never aborts the run
and is not retried within the same run.
"""

from datetime import timedelta
from uuid import UUID

from loguru import logger
from sqlalchemy import and_, exists, not_, or_, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models.conversation import (
    AttachmentStatus,
    MessageAttachment,
    Ticket,
    TicketStatus,
)
from app.db.session import SessionLocal
from app.utils.cloudinary_client import cloudinary_manager
from app.utils.datetime import utcnow_aware

BATCH_SIZE = 200
MAX_BATCHES = 10
ACTIVE_GRACE = timedelta(days=7)
_LIVE_TICKET = (TicketStatus.OPEN, TicketStatus.IN_PROGRESS, TicketStatus.MISSED)


def _due_query(now, skip: set[UUID]):
    live_ticket = exists().where(
        and_(
            Ticket.session_id == MessageAttachment.session_id,
            Ticket.status.in_(_LIVE_TICKET),
        )
    )
    stmt = (
        select(MessageAttachment)
        .where(
            MessageAttachment.status == AttachmentStatus.LINKED,
            MessageAttachment.expires_at < now,
            or_(not_(live_ticket), MessageAttachment.expires_at < now - ACTIVE_GRACE),
        )
        .order_by(MessageAttachment.expires_at)
        .limit(BATCH_SIZE)
    )
    if skip:
        stmt = stmt.where(MessageAttachment.id.notin_(skip))
    return stmt


async def expire_attachments(ctx: dict) -> int:
    if not settings.ATTACHMENT_RETENTION_ENABLED:
        logger.info("Attachment retention disabled; skipping")
        return 0
    db: Session = SessionLocal()
    expired = 0
    failed: set[UUID] = set()
    try:
        now = utcnow_aware()
        for _ in range(MAX_BATCHES):
            rows = db.execute(_due_query(now, failed)).scalars().all()
            if not rows:
                break
            for att in rows:
                kind = getattr(att.kind, "value", att.kind)
                try:
                    ok, err = cloudinary_manager.destroy(
                        att.cloudinary_public_id, resource_type=kind
                    )
                except Exception as e:  # defensive: destroy returns (ok, err) today
                    ok, err = False, type(e).__name__
                if not ok:
                    logger.warning(
                        "Retention destroy failed for public_id={}: {}; retry next run",
                        att.cloudinary_public_id,
                        err,
                    )
                    failed.add(att.id)
                    continue
                att.status = AttachmentStatus.EXPIRED
                att.url = None
                att.poster_url = None
                db.commit()
                expired += 1
        logger.info(
            "Attachment retention: {} expired, {} deferred to next run",
            expired,
            len(failed),
        )
        return expired
    except Exception as e:
        logger.error("Error in expire_attachments: {}", type(e).__name__, exc_info=True)
        db.rollback()
        return expired
    finally:
        db.close()
