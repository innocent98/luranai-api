"""Customer screen recordings: signed direct upload + verified registration (spec §4.3)."""

import math
import re
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.v1.endpoints.chat import _upload_response
from app.core.config import settings
from app.db.models.conversation import (
    AttachmentDirection,
    AttachmentKind,
    AttachmentModeration,
    AttachmentStatus,
    ConversationSession,
    MessageAttachment,
)
from app.db.models.tenant import Tenant
from app.db.session import get_db
from app.services.screen_recording.gating import screen_recording_allowed
from app.utils.cloudinary_client import cloudinary_manager
from app.utils.http_rate_limiter import (
    chat_video_register_rate_limit,
    chat_video_sign_rate_limit,
)

router = APIRouter()

MAX_VIDEOS_PER_SESSION = 3
ALLOWED_VIDEO_FORMATS = {"webm", "mp4", "mov"}
_DURATION_SLACK_S = 1.0


class SignVideoRequest(BaseModel):
    session_id: UUID


class RegisterVideoRequest(BaseModel):
    session_id: UUID
    public_id: str = Field(..., min_length=1, max_length=255)


def _err(code: int, key: str, message: str, **extra) -> HTTPException:
    return HTTPException(code, detail={"code": key, "message": message, **extra})


def _gated_session(db: Session, session_id: UUID) -> ConversationSession:
    session = db.get(ConversationSession, session_id)
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Session not found")
    tenant = db.get(Tenant, session.tenant_id)
    if not screen_recording_allowed(db, tenant):
        raise _err(403, "screen_recording_disabled", "Screen recording is not enabled")
    return session


def _opt_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _session_video_count(db: Session, session_id: UUID) -> int:
    return db.execute(
        select(func.count())
        .select_from(MessageAttachment)
        .where(
            MessageAttachment.session_id == session_id,
            MessageAttachment.kind == AttachmentKind.VIDEO,
        )
    ).scalar_one()


def _existing_video(db: Session, public_id: str) -> MessageAttachment | None:
    return db.execute(
        select(MessageAttachment).where(
            MessageAttachment.cloudinary_public_id == public_id,
            MessageAttachment.kind == AttachmentKind.VIDEO,
        )
    ).scalar_one_or_none()


@router.post("/video/sign", dependencies=[Depends(chat_video_sign_rate_limit)])
def sign_video(body: SignVideoRequest, db: Session = Depends(get_db)) -> dict:
    session = _gated_session(db, body.session_id)
    used = _session_video_count(db, session.id)
    if used >= MAX_VIDEOS_PER_SESSION:
        raise _err(429, "video_session_limit", "Recording limit reached for this conversation")
    signed = cloudinary_manager.sign_video_upload(session.tenant_id, session.id)
    if signed is None:
        raise _err(503, "video_unavailable", "Video upload is not available")
    return {
        "upload_url": signed["upload_url"],
        "fields": signed["fields"],
        "max_bytes": settings.SCREEN_RECORDING_MAX_BYTES,
        "max_duration_s": settings.SCREEN_RECORDING_MAX_SECONDS,
    }


@router.post("/video/register", dependencies=[Depends(chat_video_register_rate_limit)])
def register_video(body: RegisterVideoRequest, db: Session = Depends(get_db)) -> dict:
    session = _gated_session(db, body.session_id)
    folder = cloudinary_manager.video_folder(session.tenant_id, session.id)
    # Ownership first: never look up or destroy an asset outside this session's folder.
    # Exact allowlist of what the signer issues (<folder>/<uuid4 hex>); a prefix test is
    # bypassable because the Cloudinary SDK URL-unquotes the id (%2e%2e traversal).
    if not re.fullmatch(re.escape(folder) + r"/[0-9a-f]{32}", body.public_id):
        raise _err(422, "video_invalid", "We couldn't process that video")

    existing = _existing_video(db, body.public_id)
    if existing is not None:
        raise _err(409, "already_registered", "Already registered", attachment_id=str(existing.id))

    try:
        res = cloudinary_manager.get_video_resource(body.public_id)
    except Exception as exc:
        # Transient lookup failure: do NOT destroy a possibly-valid asset.
        logger.exception("Cloudinary video lookup failed")
        raise _err(503, "video_unavailable", "Video upload is not available") from exc
    if res is None:
        raise _err(422, "video_invalid", "We couldn't process that video")

    def _reject(key: str, message: str) -> HTTPException:
        ok, _ = cloudinary_manager.destroy(body.public_id, resource_type="video")
        if not ok:
            logger.warning("Rejected video destroy failed; orphan sweep will retry")
        return _err(422, key, message)

    if res.get("resource_type") != "video" or res.get("format") not in ALLOWED_VIDEO_FORMATS:
        raise _reject("video_invalid", "We couldn't process that video")
    try:
        duration = float(res.get("duration") or 0)
        size = int(res.get("bytes") or 0)
        if not math.isfinite(duration):
            raise ValueError("non-finite duration")
    except (TypeError, ValueError, OverflowError) as exc:
        raise _reject("video_invalid", "We couldn't process that video") from exc
    if duration <= 0 or duration > settings.SCREEN_RECORDING_MAX_SECONDS + _DURATION_SLACK_S:
        raise _reject("video_too_long", "Videos can be up to 60 seconds")
    if size <= 0 or size > settings.SCREEN_RECORDING_MAX_BYTES:
        raise _reject("video_too_large", "Videos can be up to 50 MB")
    if _session_video_count(db, session.id) >= MAX_VIDEOS_PER_SESSION:
        cloudinary_manager.destroy(body.public_id, resource_type="video")
        raise _err(429, "video_session_limit", "Recording limit reached for this conversation")

    mp4_url, poster_url = cloudinary_manager.video_delivery_urls(body.public_id, res.get("version"))
    row = MessageAttachment(
        session_id=session.id,
        tenant_id=session.tenant_id,
        direction=AttachmentDirection.INBOUND,
        status=AttachmentStatus.UPLOADED,
        moderation_status=AttachmentModeration.NOT_APPLICABLE,
        kind=AttachmentKind.VIDEO,
        cloudinary_public_id=body.public_id,
        url=mp4_url,
        poster_url=poster_url,
        content_type="video/mp4",
        source_format=res.get("format"),
        duration_ms=int(round(duration * 1000)),
        width=_opt_int(res.get("width")),
        height=_opt_int(res.get("height")),
        bytes=size,
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError as exc:
        # Concurrent double-register lost the race on the partial unique index.
        db.rollback()
        winner = _existing_video(db, body.public_id)
        if winner is None:
            raise
        raise _err(
            409, "already_registered", "Already registered", attachment_id=str(winner.id)
        ) from exc
    db.refresh(row)
    return _upload_response(row)
