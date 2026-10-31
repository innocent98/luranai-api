"""Offering photos: validate, moderate (fail-closed), optimise on ingest, cap, order, delete.

Order of work for an upload, cheapest first: write gate, archived check, per-item cap (read),
size, real-format check, then (connection released) moderation, Cloudinary upload. The cap is re-checked under a row lock on the parent item
just before the insert; if a concurrent upload took the last slot meanwhile, the asset just
uploaded is destroyed, so nothing leaks. Positions are rewritten in two steps because the
`(offering_id, position)` unique constraint is not deferrable (see the model).
"""

from __future__ import annotations

import io
from typing import Any
from uuid import UUID

from fastapi.concurrency import run_in_threadpool
from loguru import logger
from PIL import Image
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.db.models.offering import OfferingImage, OfferingStatus
from app.services.image_moderation import moderate_image
from app.services.offering_media import destroy_assets
from app.services.offering_service import OfferingError, OfferingService, image_to_dict
from app.utils.cloudinary_client import cloudinary_manager

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_DIMENSION = 9000  # an untouched 12 MP phone photo is 4,032 px; Cloudinary shrinks it anyway
ALLOWED_FORMATS = ["JPEG", "PNG", "WEBP"]
_CONTENT_TYPES = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}
_HEIC_BRANDS = (b"heic", b"heix", b"hevc", b"heim", b"heis", b"mif1", b"msf1")
_POSITION_OFFSET = 100_000

BLOCKED_MESSAGE = "This image can't be accepted. Please try a different one."


def _image_invalid(message: str) -> OfferingError:
    return OfferingError(422, "image_invalid", message)


def _storage_unavailable() -> OfferingError:
    return OfferingError(
        503,
        "image_storage_unavailable",
        "Photo storage is unavailable right now. Try again in a moment.",
    )


def _item_archived() -> OfferingError:
    return OfferingError(
        409,
        "offering_archived",
        "This item is archived. Restore it before adding photos.",
    )


def _image_limit(limit: int, used: int) -> OfferingError:
    return OfferingError(
        409,
        "offering_image_limit",
        f"Your plan allows {limit} photos per item. Remove one, or upgrade, to add more.",
        limit=limit,
        used=used,
    )


def _looks_like_heic(content: bytes) -> bool:
    return content[4:8] == b"ftyp" and content[8:12] in _HEIC_BRANDS


class OfferingImageService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.offerings = OfferingService(db)

    def _count(self, offering_id: UUID) -> int:
        return int(
            self.db.execute(
                select(func.count())
                .select_from(OfferingImage)
                .where(OfferingImage.offering_id == offering_id)
            ).scalar_one()
        )

    def _rewrite_positions(self, offering_id: UUID, ordered_ids: list[UUID]) -> None:
        """Positions 0..n-1 in the given order. Two steps: park every row out of the way, then
        place each, so the non-deferrable unique constraint never sees a clash."""
        self.db.execute(
            update(OfferingImage)
            .where(OfferingImage.offering_id == offering_id)
            .values(position=OfferingImage.position + _POSITION_OFFSET)
        )
        for position, image_id in enumerate(ordered_ids):
            self.db.execute(
                update(OfferingImage).where(OfferingImage.id == image_id).values(position=position)
            )

    def _ordered(self, offering_id: UUID) -> list[OfferingImage]:
        return list(
            self.db.execute(
                select(OfferingImage)
                .where(OfferingImage.offering_id == offering_id)
                .order_by(OfferingImage.position)
            ).scalars()
        )

    def _admit(self, tenant_id: UUID, offering_id: UUID, content: bytes) -> tuple[UUID, int, str]:
        """Everything that is blocking and needs the database, up to the point of moderation.

        Returns (item id, per-item photo limit, content type) and releases the connection, so no
        transaction is held open while the image is moderated and uploaded.
        """
        entitlements = self.offerings.require_write(tenant_id)
        item = self.offerings.get_row(tenant_id, offering_id)
        if item.status == OfferingStatus.ARCHIVED:
            raise _item_archived()
        item_id = item.id
        limit = int(entitlements.value("max_offering_images") or 0)
        used = self._count(item_id)
        if used >= limit:
            raise _image_limit(limit, used)

        if len(content) > MAX_UPLOAD_BYTES:
            raise OfferingError(
                413, "image_too_large", "The photo is larger than 5 MB. Choose a smaller one."
            )
        ok, error = cloudinary_manager.validate_image(
            content,
            max_size_mb=5,
            allowed_formats=ALLOWED_FORMATS,
            max_dimension=MAX_DIMENSION,
        )
        if not ok:
            if _looks_like_heic(content):
                raise _image_invalid(
                    "HEIC photos are not supported. Use a JPEG, PNG or WEBP photo "
                    "(on iPhone, choose 'Most Compatible' or pick the photo from the gallery)."
                )
            # "Invalid image file: <library exception text>" is not for people: say it plainly.
            if not error or error.startswith("Invalid image file"):
                error = "The file is not a valid JPEG, PNG or WEBP image."
            raise _image_invalid(error)

        with Image.open(io.BytesIO(content)) as probe:
            content_type = _CONTENT_TYPES.get(probe.format or "", "image/jpeg")
        self.db.commit()
        return item_id, limit, content_type

    def _store(
        self, tenant_id: UUID, offering_id: UUID, limit: int, info: dict[str, Any]
    ) -> dict[str, Any]:
        """Serialise on the parent item, re-check the cap and the item, take the next position.

        If the photo cannot be stored (cap filled meanwhile, item archived or deleted meanwhile), the
        asset just uploaded is destroyed so nothing leaks.
        """
        try:
            locked = self.offerings.get_row_locked(tenant_id, offering_id)
        except OfferingError:
            # Deleted while the photo uploaded: nothing to attach it to. Destroy it right away.
            self.db.commit()
            destroy_assets([info["public_id"]])
            raise
        used = self._count(locked.id)
        refusal = None
        if locked.status == OfferingStatus.ARCHIVED:
            refusal = _item_archived()
        elif used >= limit:
            refusal = _image_limit(limit, used)
        if refusal is not None:
            self.db.commit()  # nothing written: just release the row lock
            destroy_assets([info["public_id"]])
            raise refusal
        position = int(
            self.db.execute(
                select(func.coalesce(func.max(OfferingImage.position), -1)).where(
                    OfferingImage.offering_id == locked.id
                )
            ).scalar_one()
        )
        image = OfferingImage(
            offering_id=locked.id,
            tenant_id=tenant_id,
            url=info["url"],
            cloudinary_public_id=info["public_id"],
            position=position + 1,
            width=info.get("width"),
            height=info.get("height"),
            bytes=info.get("bytes"),
        )
        self.db.add(image)
        self.offerings.record_activity(tenant_id)
        try:
            self.db.commit()
        except Exception:
            self.db.rollback()
            destroy_assets([info["public_id"]])
            raise
        self.db.refresh(image)
        return image_to_dict(image)

    async def upload(self, tenant_id: UUID, offering_id: UUID, content: bytes) -> dict[str, Any]:
        """Runs on the event loop, so every blocking step (database, Pillow, Cloudinary) is handed
        to the thread pool and only the moderation call, which is itself async, awaits here."""
        item_id, limit, content_type = await run_in_threadpool(
            self._admit, tenant_id, offering_id, content
        )

        # Moderation precedes storage: a flagged image is never uploaded. Fail closed, and never
        # tell the caller why.
        try:
            allowed, _reason = await moderate_image(content, content_type)
        except Exception as exc:  # noqa: BLE001 - fail-closed
            logger.error("Offering image moderation raised (fail-closed): {}", type(exc).__name__)
            allowed = False
        if not allowed:
            raise OfferingError(422, "image_blocked", BLOCKED_MESSAGE)

        info, upload_error = await run_in_threadpool(
            cloudinary_manager.upload_offering_image, content, tenant_id
        )
        if info is None or not info.get("url") or not info.get("public_id"):
            logger.error("Offering image upload failed: {}", upload_error)
            raise _storage_unavailable()
        return await run_in_threadpool(self._store, tenant_id, item_id, limit, info)

    def reorder(self, tenant_id: UUID, offering_id: UUID, image_ids: list[UUID]) -> dict[str, Any]:
        self.offerings.require_write(tenant_id)
        item = self.offerings.get_row_locked(tenant_id, offering_id)
        current = {image.id for image in self._ordered(item.id)}
        if len(image_ids) != len(set(image_ids)) or set(image_ids) != current:
            self.db.commit()  # nothing written: just release the row lock
            raise OfferingError(
                422,
                "image_order_mismatch",
                "The list must contain each of this item's photos exactly once.",
            )
        self._rewrite_positions(item.id, image_ids)
        self.offerings.record_activity(tenant_id)
        self.db.commit()
        self.db.expire_all()
        return self.offerings.serialize(self.offerings.get_row(tenant_id, offering_id))

    def delete(self, tenant_id: UUID, offering_id: UUID, image_id: UUID) -> None:
        """Delete one photo (the next one becomes the cover) and destroy its asset."""
        self.offerings.require_write(tenant_id)
        item = self.offerings.get_row_locked(tenant_id, offering_id)
        images = self._ordered(item.id)
        target = next((i for i in images if i.id == image_id), None)
        if target is None:
            self.db.commit()  # nothing written: just release the row lock
            raise OfferingError(404, "offering_image_not_found", "Photo not found")
        public_id = target.cloudinary_public_id
        self.db.delete(target)
        self.db.flush()
        self._rewrite_positions(item.id, [i.id for i in images if i.id != image_id])
        self.offerings.record_activity(tenant_id)
        self.db.commit()
        destroy_assets([public_id])
