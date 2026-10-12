"""Daily chat-attachment retention (spec SR-D5, §4.5).

LINKED attachments past expires_at have their Cloudinary asset destroyed. The row is
kept as EXPIRED with url/poster_url NULL, so transcripts show a placeholder. An
attachment whose session has a live ticket (open / in_progress / missed) is deferred,
but never for more than ACTIVE_GRACE past expiry. One failing row never aborts the run
and is not retried within the same run.
"""

import asyncio
from datetime import timedelta
from uuid import UUID

from loguru import logger
from sqlalchemy import and_, exists, not_, or_, select, update
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
MAX_CONSECUTIVE_FAILURES = 20
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
    skipped = 0
    consecutive_failures = 0
    breaker_tripped = False
    failed: set[UUID] = set()
    try:
        now = utcnow_aware()
        for _ in range(MAX_BATCHES):
            rows = db.execute(_due_query(now, failed)).scalars().all()
            # Snapshot plain values: commits expire ORM objects (N+1 refreshes, and
            # ObjectDeletedError if a purge cascade-deleted a row mid-run).
            batch = [(a.id, getattr(a.kind, "value", a.kind), a.cloudinary_public_id) for a in rows]
            if not batch:
                break
            for pos, (att_id, kind, public_id) in enumerate(batch):
                try:
                    # Blocking SDK call: keep it off the ARQ event loop.
                    ok, err = await asyncio.to_thread(
                        cloudinary_manager.destroy, public_id, resource_type=kind
                    )
                except Exception as e:  # defensive: destroy returns (ok, err) today
                    ok, err = False, type(e).__name__
                if not ok:
                    logger.warning(
                        "Retention destroy failed for public_id={}: {}; retry next run",
                        public_id,
                        err,
                    )
                    failed.add(att_id)
                    consecutive_failures += 1
                    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        logger.error(
                            "Attachment retention: {} consecutive destroy failures; "
                            "ending run with {} rows left in current batch",
                            consecutive_failures,
                            len(batch) - pos - 1,
                        )
                        breaker_tripped = True
                        break
                    continue
                consecutive_failures = 0
                try:
                    # If this commit fails after a successful destroy, the next run's
                    # destroy returns "not found", which counts as ok (self-heals).
                    res = db.execute(
                        update(MessageAttachment)
                        .where(
                            MessageAttachment.id == att_id,
                            MessageAttachment.status == AttachmentStatus.LINKED,
                        )
                        .values(status=AttachmentStatus.EXPIRED, url=None, poster_url=None)
                    )
                    db.commit()
                except Exception as e:
                    db.rollback()
                    failed.add(att_id)
                    logger.error(
                        "Retention update failed for public_id={}: {}",
                        public_id,
                        type(e).__name__,
                    )
                    continue
                if res.rowcount:
                    expired += 1
                else:
                    skipped += 1  # row vanished or changed state mid-run
            if breaker_tripped:
                break
        logger.info(
            "Attachment retention: {} expired, {} skipped, {} deferred to next run",
            expired,
            skipped,
            len(failed),
        )
        return expired
    except Exception as e:
        logger.error("Error in expire_attachments: {}", type(e).__name__)
        db.rollback()
        return expired
    finally:
        db.close()
