"""Daily cron that reaps orphaned offering photos.

A catalog photo is uploaded to Cloudinary and then its row is inserted in the same request. If the
process dies between the two, or a best-effort destroy failed when a photo or item was deleted, an
asset is left with no row. Every offering photo carries an environment-scoped tag, so this job
lists the tag, and destroys assets older than 24 hours that no `offering_images` row references.
The 24 hours keep it clear of an upload that is still in flight.
"""

import asyncio
from datetime import UTC, datetime, timedelta

from loguru import logger
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.db.models.offering import OfferingImage, OfferingMiss
from app.db.session import SessionLocal
from app.utils.cloudinary_client import cloudinary_manager, offering_sweep_tag

ORPHAN_MAX_AGE = timedelta(hours=24)


def _created(asset: dict) -> datetime | None:
    raw = asset.get("created_at")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


async def sweep_orphan_offering_images(ctx: dict) -> int:
    """Destroy tagged offering assets older than 24 h that no row references. Returns the count."""
    db: Session = SessionLocal()
    try:
        cutoff = datetime.now(UTC) - ORPHAN_MAX_AGE
        destroyed = 0
        cursor: str | None = None
        while True:
            assets, cursor = await asyncio.to_thread(
                cloudinary_manager.list_tagged_images, offering_sweep_tag(), cursor
            )
            old = {
                a["public_id"]: a
                for a in assets
                if a.get("public_id") and (_created(a) or datetime.now(UTC)) < cutoff
            }
            if old:
                referenced = set(
                    db.execute(
                        select(OfferingImage.cloudinary_public_id).where(
                            OfferingImage.cloudinary_public_id.in_(list(old))
                        )
                    ).scalars()
                )
                for public_id in old:
                    if public_id in referenced:
                        continue
                    try:
                        ok, err = await asyncio.to_thread(
                            cloudinary_manager.destroy, public_id, resource_type="image"
                        )
                    except Exception as exc:  # defensive: destroy returns (ok, err)
                        ok, err = False, type(exc).__name__
                    if ok:
                        destroyed += 1
                    else:
                        logger.warning(
                            "Orphan offering asset destroy failed ({}); retry next run", err
                        )
            if not cursor:
                break
        logger.info("Orphan offering image sweep: {} destroyed", destroyed)
        return destroyed
    except Exception as exc:
        logger.error("Error in sweep_orphan_offering_images: {}", type(exc).__name__)
        return 0
    finally:
        db.close()


MISS_RETENTION = timedelta(days=90)


async def sweep_old_offering_misses(ctx: dict) -> int:
    """Delete Not-found terms nobody has asked for in 90 days. Returns the number deleted."""
    db: Session = SessionLocal()
    try:
        cutoff = datetime.now(UTC) - MISS_RETENTION
        result = db.execute(delete(OfferingMiss).where(OfferingMiss.last_seen_at < cutoff))
        db.commit()
        logger.info("Offering miss sweep: {} deleted", result.rowcount)
        return int(result.rowcount)
    except Exception as exc:
        logger.error("Error in sweep_old_offering_misses: {}", type(exc).__name__)
        db.rollback()
        return 0
    finally:
        db.close()
