"""Daily cron that reaps orphaned chat-image uploads.

Uploading a chat image is two-step (upload, then send). A visitor can upload
and never send, leaving a ``message_attachments`` row with
``status == UPLOADED`` and ``message_id IS NULL`` plus a Cloudinary asset.
This job deletes such rows older than 24 hours and their Cloudinary assets.
If a Cloudinary delete fails, the row is kept (retried next run) and the batch continues.
"""

import asyncio
from datetime import UTC, datetime, timedelta

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.conversation import AttachmentStatus, MessageAttachment
from app.db.session import SessionLocal
from app.utils.cloudinary_client import cloudinary_manager

ORPHAN_MAX_AGE = timedelta(hours=24)


async def cleanup_orphan_attachments(ctx: dict) -> int:
    """Delete UPLOADED attachments older than 24h (and their Cloudinary assets).

    Returns the number of attachment rows deleted.
    """
    db: Session = SessionLocal()
    try:
        cutoff = datetime.now(UTC) - ORPHAN_MAX_AGE
        orphans = (
            db.execute(
                select(MessageAttachment).where(
                    MessageAttachment.status == AttachmentStatus.UPLOADED,
                    MessageAttachment.created_at < cutoff,
                )
            )
            .scalars()
            .all()
        )

        deleted = 0
        for att in orphans:
            # destroy() returns (ok, err) rather than raising; keep the row on
            # failure so the next daily run retries the Cloudinary delete.
            try:
                ok, err = await asyncio.to_thread(
                    cloudinary_manager.destroy,
                    att.cloudinary_public_id,
                    resource_type=att.kind.value,
                )
            except Exception as e:  # defensive: future destroy impls may raise
                ok, err = False, str(e)
            if not ok:
                logger.warning(
                    "Cloudinary destroy failed for orphan attachment {} (public_id={}): {}; "
                    "keeping row for retry",
                    att.id,
                    att.cloudinary_public_id,
                    err,
                )
                continue
            db.delete(att)
            deleted += 1

        db.commit()
        logger.info("Orphan attachment cleanup: {} deleted", deleted)
        return deleted
    except Exception as e:
        logger.error("Error in cleanup_orphan_attachments: {}", str(e), exc_info=True)
        db.rollback()
        return 0
    finally:
        db.close()
