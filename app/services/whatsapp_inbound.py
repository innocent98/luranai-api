"""WhatsApp event handling: status flips, inbound customer messages, owner-typed messages.

Tenant is resolved ONLY from `session_key` here (server side); nothing in the payload can
name a tenant. Pure functions of `(db, event)` so they are testable without arq; the arq
job in `app.workers.whatsapp` is a thin wrapper.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from datetime import timedelta
from uuid import UUID

from fastapi import HTTPException
from loguru import logger
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.channels import identity
from app.channels.base import MediaNotFound, MediaTooLarge
from app.channels.registry import get_adapter
from app.core.config import settings
from app.db.models.conversation import (
    AttachmentDirection,
    AttachmentStatus,
    ConversationSession,
    MessageAttachment,
    MessageRole,
    SessionStatus,
)
from app.db.models.review import RatingRequest
from app.db.models.tenant import Tenant, TenantCapabilities
from app.db.models.visitor import Visitor, VisitorSource
from app.db.models.whatsapp import (
    WhatsAppChat,
    WhatsAppConnection,
    WhatsAppInboundMessage,
    WhatsAppStatus,
)
from app.schemas.conversation import ConversationSessionCreate, MessageRequest
from app.schemas.explore import ConversationSource
from app.services import whatsapp_enqueue
from app.services.review_service import RatingSubmitError, ReviewService
from app.services.conversation_service import (
    ConversationService,
    message_channel,
    new_session_attribution,
)
from app.utils.datetime import ensure_utc_aware, utcnow_aware, utcnow_naive

# Fixed fallback for what Luran cannot read (voice notes, video, documents, stickers).
OTHER_KIND_REPLY = (
    "Sorry, I can only read text messages and images for now. Please type your question "
    "and I'll help."
)
# Transcript stand-in for what could not be read: agents must see that something arrived.
UNSUPPORTED_MEDIA_PLACEHOLDER = (
    "[The customer sent a voice note, video, document or sticker. Luran can't read these on "
    "WhatsApp yet.]"
)
IMAGE_UNREADABLE_PLACEHOLDER = "[The customer sent an image that could not be received.]"
IMAGE_UNREADABLE_REPLY = "Sorry, I couldn't open that image. Could you send it again?"
IMAGE_TOO_LARGE_REPLY = "Sorry, that image is too large for me to open. Please send a smaller one."
# The ONLY thing a customer ever hears about a moderated image: never the moderation reason.
IMAGE_DISABLED_REPLY = "Sorry, I can't take images in this chat. Please type your question."
IMAGE_RATE_MAX = 10  # images per conversation per window
IMAGE_RATE_WINDOW = timedelta(hours=1)
IMAGE_BLOCKED_REPLY = "Sorry, I can't accept that image. Please send a different one."
IMAGE_ONLY_PLACEHOLDER = "[image]"  # same stand-in chat.py stores for an image without a caption
RATING_REPLY = re.compile(r"\s*([1-5])(\s*(/\s*5|stars?|⭐+))?\s*[.!]*\s*", re.IGNORECASE)
_IMAGE_MIMES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
PLAN_ALERT_DEBOUNCE = timedelta(hours=6)

_USABLE_SESSION = (SessionStatus.ACTIVE, SessionStatus.ESCALATED, SessionStatus.ABANDONED)

_DISCONNECT_REASONS = {
    401: "logged_out_from_phone",
    440: "connection_replaced",
    515: "restart_required",
}


def get_connection_by_session_key(db: Session, session_key: str) -> WhatsAppConnection | None:
    return db.execute(
        select(WhatsAppConnection).where(WhatsAppConnection.session_key == session_key)
    ).scalar_one_or_none()


# --------------------------------------------------------------------------- dispatch
async def handle_event(db: Session, event: dict) -> str:
    """Process one validated webhook event. Returns a short outcome label (for tests/logs)."""
    conn = get_connection_by_session_key(db, event["session_key"])
    if conn is None:
        logger.info("whatsapp event dropped: unknown session")
        return "dropped_unknown_session"
    etype = event["type"]
    if etype == "session.status":
        return handle_status(db, conn, event)
    if etype == "message.inbound":
        return await handle_inbound(db, conn, event)
    if etype == "message.outbound_external":
        return await handle_outbound_external(db, conn, event)
    return "ignored_type"


# --------------------------------------------------------------------------- status
def handle_status(db: Session, conn: WhatsAppConnection, event: dict) -> str:
    """Flip the connection status from a sidecar report. Never re-pairs automatically."""
    try:
        status = WhatsAppStatus(event["status"])
    except ValueError:
        logger.warning("whatsapp status event with unknown status ignored")
        return "ignored_unknown_status"
    now = utcnow_aware()
    conn.status = status.value
    if status == WhatsAppStatus.CONNECTED:
        conn.last_connected_at = now
    elif status in (
        WhatsAppStatus.LOGGED_OUT,
        WhatsAppStatus.FAILED,
        WhatsAppStatus.RECONNECTING,
    ):
        code = event.get("code")
        conn.last_disconnect_at = now
        conn.last_disconnect_reason = _DISCONNECT_REASONS.get(
            code, f"closed_{code}" if code is not None else status.value
        )
        if status in (WhatsAppStatus.LOGGED_OUT, WhatsAppStatus.FAILED):
            # TODO(beta): email tenant admins + in-app notice (debounced, not for deliberate
            # disconnects). The demo only records it.
            logger.warning("whatsapp connection {} went {}", conn.id, status.value)
    db.commit()
    return f"status_{status.value}"


# --------------------------------------------------------------------------- helpers
def _new_visitor(db: Session, tenant_id: UUID, push_name: str | None) -> Visitor:
    now = utcnow_aware()
    visitor = Visitor(
        tenant_id=tenant_id,
        public_token=secrets.token_urlsafe(32),
        first_seen_at=now,
        last_seen_at=now,
        created_from=VisitorSource.WHATSAPP,
        # NOTE: no `phone`: the Visitor listener would store an UNSALTED SHA-256 of it.
        name=(push_name or "").strip()[:255] or None,
    )
    db.add(visitor)
    db.flush()
    return visitor


def _get_or_create_chat(
    db: Session, conn: WhatsAppConnection, chat_ref: str
) -> tuple[WhatsAppChat, bool]:
    key = identity.contact_hmac(chat_ref)
    chat = db.execute(
        select(WhatsAppChat).where(
            WhatsAppChat.connection_id == conn.id, WhatsAppChat.contact_hmac == key
        )
    ).scalar_one_or_none()
    if chat is not None:
        return chat, False
    chat = WhatsAppChat(
        tenant_id=conn.tenant_id, connection_id=conn.id, contact_hmac=key, contact_ref=chat_ref
    )
    db.add(chat)
    db.flush()
    return chat, True


def _ensure_visitor(db: Session, conn: WhatsAppConnection, chat: WhatsAppChat, push_name) -> Visitor:  # type: ignore[no-untyped-def]
    visitor = db.get(Visitor, chat.visitor_id) if chat.visitor_id else None
    if visitor is None:
        visitor = _new_visitor(db, conn.tenant_id, push_name)
        chat.visitor_id = visitor.id
    elif push_name and not visitor.name:
        visitor.name = push_name.strip()[:255] or None
    return visitor


def _record_message_id(db: Session, conn: WhatsAppConnection, provider_message_id: str) -> bool:
    """Idempotent insert. False = already seen (duplicate delivery or replay)."""
    try:
        with db.begin_nested():
            db.add(
                WhatsAppInboundMessage(
                    connection_id=conn.id,
                    provider_message_id=provider_message_id,
                    received_at=utcnow_aware(),
                )
            )
        return True
    except IntegrityError:
        return False


def _forget_message_id(db: Session, conn: WhatsAppConnection, provider_message_id: str) -> None:
    db.rollback()
    db.execute(
        delete(WhatsAppInboundMessage).where(
            WhatsAppInboundMessage.connection_id == conn.id,
            WhatsAppInboundMessage.provider_message_id == provider_message_id,
        )
    )
    db.commit()


def _usable_session(db: Session, conn: WhatsAppConnection, chat: WhatsAppChat):  # type: ignore[no-untyped-def]
    """The chat's current conversation unless it is resolved (then a new one starts)."""
    if not chat.session_id:
        return None
    session = db.get(ConversationSession, chat.session_id)
    if session is None or session.tenant_id != conn.tenant_id:
        return None
    return session if session.status in _USABLE_SESSION else None


def _ensure_session(
    db: Session, conn: WhatsAppConnection, chat: WhatsAppChat, user_identifier: str
) -> ConversationSession:
    """Existing usable session, else a new WhatsApp-sourced one (store-only paths)."""
    session = _usable_session(db, conn, chat)
    if session is not None:
        if session.status == SessionStatus.ABANDONED:
            session.status = SessionStatus.ACTIVE
            db.commit()
        return session
    created = ConversationService(db).create_session(
        ConversationSessionCreate(
            tenant_id=conn.tenant_id,
            user_identifier=user_identifier,
            visitor_id=chat.visitor_id,
            source=ConversationSource.WHATSAPP.value,
            source_meta={"channel_connection_id": str(conn.id), "provider": conn.provider},
        )
    )
    chat.session_id = created.id
    db.commit()
    return db.get(ConversationSession, created.id)  # type: ignore[return-value]


def _plan_blocks_conversation(db: Session, conn: WhatsAppConnection, *, new_session: bool) -> bool:
    """The plan checks `send_message` applies before it will talk to a customer, for the paths
    that persist without going through it: an inactive subscription / expired trial blocks every
    message, and the monthly conversation cap blocks creating a NEW conversation. Read-only."""
    tenant = db.get(Tenant, conn.tenant_id)
    if tenant is None:
        return True
    if tenant.subscription_status in {"trial_expired", "canceled", "past_due"}:
        return True
    if tenant.subscription_status in {"trialing", "trial"}:
        ends = ensure_utc_aware(tenant.trial_ends_at)
        if ends is not None and utcnow_aware() > ends:
            return True
    if not new_session:
        return False
    caps = db.execute(
        select(TenantCapabilities).where(TenantCapabilities.tenant_id == conn.tenant_id)
    ).scalar_one_or_none()
    limit = caps.max_conversations_per_month if caps is not None else None
    if limit is None:
        return False
    month_start = utcnow_naive().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    used = db.execute(
        select(func.count())
        .select_from(ConversationSession)
        .where(
            ConversationSession.tenant_id == conn.tenant_id,
            ConversationSession.created_at >= month_start,
        )
    ).scalar_one()
    return used >= limit


def _tenant_may_use(db: Session, tenant_id: UUID) -> bool:
    caps = db.execute(
        select(TenantCapabilities).where(TenantCapabilities.tenant_id == tenant_id)
    ).scalar_one_or_none()
    return settings.WHATSAPP_ENABLED and bool(caps and caps.can_use_whatsapp)


async def _enqueue_reply(conn: WhatsAppConnection, chat: WhatsAppChat, text: str, key: str) -> None:
    await whatsapp_enqueue.enqueue_send(conn.id, chat.id, text, key, guard=True)


# --------------------------------------------------------------------------- inbound
async def handle_inbound(db: Session, conn: WhatsAppConnection, event: dict) -> str:
    from app.api.v1.endpoints.chat import _fanout_customer_message, send_message
    from app.db.models.conversation import Message

    if not _tenant_may_use(db, conn.tenant_id):  # kill switch / revoked grant
        return "dropped_disabled"

    message_id = event["message_id"]
    if not _record_message_id(db, conn, message_id):
        return "duplicate"
    db.commit()

    try:
        now = utcnow_aware()
        chat, _created = _get_or_create_chat(db, conn, event["from_ref"])
        chat.last_inbound_at = now
        if event.get("from_ref") and chat.contact_ref != event["from_ref"]:
            chat.contact_ref = event["from_ref"]
        db.commit()

        human_until = ensure_utc_aware(chat.human_until)
        paused = human_until is not None and human_until > now
        text = (event.get("text") or "").strip()
        kind = event["kind"]

        if kind == "text" and text:
            rated = await _maybe_record_rating(db, conn, chat, text)
            if rated is not None:
                return rated

        if kind == "image" and event.get("media"):
            return await _handle_image(db, conn, chat, event, paused, text)

        if kind != "text" or not text:
            # No automatic replies while a human owns the chat: the owner typed from the phone,
            # AI is off, or a support agent is assigned / the conversation is escalated.
            silent = paused or not conn.ai_enabled or _human_owns(db, conn, chat)
            too_large = event.get("skipped") == "media_too_large"
            placeholder = (
                IMAGE_UNREADABLE_PLACEHOLDER if too_large else UNSUPPORTED_MEDIA_PLACEHOLDER
            )
            reply = IMAGE_TOO_LARGE_REPLY if too_large else OTHER_KIND_REPLY
            if _plan_blocks_conversation(
                db, conn, new_session=_usable_session(db, conn, chat) is None
            ):
                # Same rule as a text message that gets HTTP 402: store only into an existing
                # conversation, never open one, say nothing to the customer, alert the tenant.
                return await _plan_limit(
                    db, conn, chat, placeholder, f"wa_{chat.contact_hmac[:24]}"
                )
            if not silent:
                # Enqueue first: the send is idempotent on its key, so an arq retry after a
                # storage failure cannot double-send, and a failed enqueue stores nothing.
                await _enqueue_reply(conn, chat, reply, f"other:{message_id}")
            _ensure_visitor(db, conn, chat, event.get("push_name"))
            db.commit()
            await _store_non_text(
                db,
                conn,
                chat,
                f"wa_{chat.contact_hmac[:24]}",
                placeholder,
                reply if not silent else None,
                _fanout_customer_message,
                Message,
            )
            return "stored_other_while_paused" if silent else "other_kind_reply"

        visitor = _ensure_visitor(db, conn, chat, event.get("push_name"))
        db.commit()
        user_identifier = f"wa_{chat.contact_hmac[:24]}"

        if paused or not conn.ai_enabled:
            return await _store_without_ai(
                db, conn, chat, text, user_identifier, paused, _fanout_customer_message, Message
            )

        session = _usable_session(db, conn, chat)
        request = MessageRequest(
            message=text,
            session_id=session.id if session else None,
            tenant_id=conn.tenant_id,
            user_identifier=user_identifier,
            metadata={"visitor_token": visitor.public_token, "entry_point": "whatsapp"},
        )
        attribution = new_session_attribution.set(
            (
                ConversationSource.WHATSAPP.value,
                {"channel_connection_id": str(conn.id), "provider": conn.provider},
            )
        )
        channel = message_channel.set("whatsapp")
        try:
            response = await send_message(request, db)
        except HTTPException as exc:
            if exc.status_code == 402:
                return await _plan_limit(db, conn, chat, text, user_identifier)
            raise
        finally:
            message_channel.reset(channel)
            new_session_attribution.reset(attribution)

        chat.session_id = response.session_id
        db.commit()
        if response.message.role == MessageRole.USER:
            return "stored_human_owns_session"  # escalated: a human answers, no AI reply
        content = (response.message.content or "").strip()
        if content:
            await _enqueue_reply(conn, chat, content, f"ai:{response.message.id}")
        return "ai_reply"
    except Exception:
        # Let arq retry: forget the dedupe row so the retry is not skipped as a duplicate.
        _forget_message_id(db, conn, message_id)
        raise


def _human_owns(db: Session, conn: WhatsAppConnection, chat: WhatsAppChat) -> bool:
    """A support agent is assigned to the chat's conversation, or it is escalated to humans."""
    session = _usable_session(db, conn, chat)
    if session is None:
        return False
    return session.status == SessionStatus.ESCALATED or session.agent_id is not None


# --------------------------------------------------------------------------- rating reply
def _clear_rating(db: Session, chat: WhatsAppChat, request_id: UUID) -> None:
    """Close `request_id` on the chat, but only if it is still the one remembered: the sender job
    does not hold the chat lock and may have just stored a newer prompt."""
    db.execute(
        update(WhatsAppChat)
        .where(WhatsAppChat.id == chat.id, WhatsAppChat.rating_request_id == request_id)
        .values(rating_request_id=None, rating_request_expires_at=None)
    )
    db.commit()
    db.refresh(chat)


async def _maybe_record_rating(
    db: Session, conn: WhatsAppConnection, chat: WhatsAppChat, text: str
) -> str | None:
    """A bare 1-5 answer to the rating prompt this chat was sent. None = ordinary text.

    The request is the one remembered on THIS chat row (written only when the prompt was
    delivered); nothing in the event names a request, session or tenant, so a reply cannot
    rate another conversation. The write goes through the same ReviewService fulfilment the
    widget uses, so agent, ticket and source attribution are identical.
    """
    match = RATING_REPLY.fullmatch(text)
    if match is None or chat.rating_request_id is None:
        return None
    open_id = chat.rating_request_id
    expires = ensure_utc_aware(chat.rating_request_expires_at)
    if expires is None or expires <= utcnow_aware():
        _clear_rating(db, chat, open_id)  # expired: ordinary text from here on
        return None
    if _plan_blocks_conversation(db, conn, new_session=False):
        return None  # the normal flow applies the plan rule to it
    request = db.get(RatingRequest, chat.rating_request_id)
    session = db.get(ConversationSession, request.session_id) if request is not None else None
    if (
        request is None
        or session is None
        or request.tenant_id != conn.tenant_id
        or session.tenant_id != conn.tenant_id
        or session.user_identifier != f"wa_{chat.contact_hmac[:24]}"  # this chat's customer
        # a request from an earlier conversation must not capture a digit typed in a new one
        or request.session_id != chat.session_id
    ):
        _clear_rating(db, chat, open_id)
        return None
    score = int(match.group(1))
    try:
        ReviewService(db).rate_request_by_id(
            session,
            request.id,
            score=score,
            user_identifier=session.user_identifier,
            meta={"channel": "whatsapp"},
        )
    except RatingSubmitError:
        # Already answered (e.g. on the widget), expired, or unusable: a replay is plain text.
        _clear_rating(db, chat, open_id)
        return None
    _clear_rating(db, chat, open_id)
    human_until = ensure_utc_aware(chat.human_until)
    if human_until is not None and human_until > utcnow_aware():
        return "rating_recorded"  # the owner is on the phone: no automatic thank-you
    try:
        await _send_system(
            conn, chat, f"Thanks! We've recorded your {score}/5.", f"rating_ack:{request.id}"
        )
    except Exception:  # noqa: BLE001 - courtesy only: the rating is committed, never redo it
        logger.exception("whatsapp rating acknowledgement not queued")
    return "rating_recorded"


async def _send_system(conn: WhatsAppConnection, chat: WhatsAppChat, text: str, key: str) -> None:
    await whatsapp_enqueue.enqueue_send(conn.id, chat.id, text, key, guard=False)


# --------------------------------------------------------------------------- inbound image
def _discard_unlinked(db: Session, attachment: MessageAttachment | None) -> None:
    """Best-effort cleanup when a failure leaves the uploaded image unlinked (a retry re-uploads)."""
    if attachment is None:
        return
    try:
        db.rollback()
        row = db.get(MessageAttachment, attachment.id)
        if row is not None and row.status == AttachmentStatus.UPLOADED and row.message_id is None:
            from app.utils.cloudinary_client import cloudinary_manager

            public_id = row.cloudinary_public_id
            db.delete(row)
            db.commit()
            cloudinary_manager.destroy(public_id, resource_type="image")
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.warning("whatsapp image cleanup failed")


async def _handle_image(
    db: Session,
    conn: WhatsAppConnection,
    chat: WhatsAppChat,
    event: dict,
    paused: bool,
    caption: str,
) -> str:
    """Customer image: fetch from the sidecar, run the web-chat image pipeline, then either the
    normal AI turn (the model sees the image) or store-only when a human owns the chat."""
    from app.api.v1.endpoints.chat import (
        _fanout_customer_message,
        screen_image_bytes,
        send_message,
        store_screened_image,
    )
    from app.db.models.conversation import Message

    message_id = event["message_id"]
    media = event["media"]
    user_identifier = f"wa_{chat.contact_hmac[:24]}"
    silent = paused or not conn.ai_enabled or _human_owns(db, conn, chat)
    new_session = _usable_session(db, conn, chat) is None

    silent_chat = silent

    # Plan / subscription guard BEFORE the bytes are fetched, screened or stored.
    if _plan_blocks_conversation(db, conn, new_session=new_session):
        return await _plan_limit(db, conn, chat, caption or IMAGE_ONLY_PLACEHOLDER, user_identifier)

    async def _unreadable(reply: str | None) -> str:
        silent = silent_chat or reply is None
        if not silent:
            await _enqueue_reply(conn, chat, reply, f"imgfail:{message_id}")
        _ensure_visitor(db, conn, chat, event.get("push_name"))
        db.commit()
        await _store_non_text(
            db,
            conn,
            chat,
            user_identifier,
            IMAGE_UNREADABLE_PLACEHOLDER,
            None if silent else reply,
            _fanout_customer_message,
            Message,
        )
        return "image_unreadable_silent" if silent else "image_unreadable"

    # 0. Same gates web chat applies to uploads: the tenant's upload switch and a per-chat rate cap
    #    (each image costs a moderation call, an upload and a vision turn).
    tenant = db.get(Tenant, conn.tenant_id)
    if tenant is None or not tenant.widget_allow_file_upload:
        return await _unreadable(IMAGE_DISABLED_REPLY)
    session_now = _usable_session(db, conn, chat)
    if session_now is not None:
        recent = db.execute(
            select(func.count())
            .select_from(MessageAttachment)
            .where(
                MessageAttachment.session_id == session_now.id,
                MessageAttachment.direction == AttachmentDirection.INBOUND,
                MessageAttachment.created_at >= utcnow_aware() - IMAGE_RATE_WINDOW,
            )
        ).scalar_one()
        if recent >= IMAGE_RATE_MAX:
            return await _unreadable(None)  # over the cap: noted for agents, no reply (no loop)

    # 1. Bytes from the sidecar's short-lived store; size and checksum must match the event.
    declared_mime = str(media.get("mime", "")).lower()
    if declared_mime not in _IMAGE_MIMES:
        return await _unreadable(IMAGE_UNREADABLE_REPLY)
    cap = min(settings.WHATSAPP_MEDIA_MAX_BYTES, 10 * 1024 * 1024)
    try:
        blob = await get_adapter(conn.provider).fetch_media(conn.session_key, media["id"], cap)
    except MediaTooLarge:
        return await _unreadable(IMAGE_TOO_LARGE_REPLY)
    except MediaNotFound:  # expired / evicted (10 min) or never existed: nothing to retry
        return await _unreadable(IMAGE_UNREADABLE_REPLY)
    # Any other channel error (sidecar down) propagates: the worker retries the event.
    content = blob.content
    if (
        len(content) != media["bytes"]
        or hashlib.sha256(content).hexdigest() != str(media["sha256"]).lower()
    ):
        logger.warning("whatsapp inbound image failed its size/checksum check")
        return await _unreadable(IMAGE_UNREADABLE_REPLY)

    # 2. Same screening as web chat (cap, real format, fail-closed moderation) BEFORE anything
    #    is stored or any conversation is opened.
    try:
        await screen_image_bytes(content, declared_mime)
    except HTTPException as exc:
        if exc.status_code == 422:  # moderation said no (or could not say yes): store nothing
            if not silent:
                await _enqueue_reply(conn, chat, IMAGE_BLOCKED_REPLY, f"imgblock:{message_id}")
            return "image_blocked"
        if exc.status_code == 413:
            return await _unreadable(IMAGE_TOO_LARGE_REPLY)
        return await _unreadable(IMAGE_UNREADABLE_REPLY)

    # 3. Store it against the conversation (opened here if this is the chat's first message).
    visitor = _ensure_visitor(db, conn, chat, event.get("push_name"))
    db.commit()
    session = _ensure_session(db, conn, chat, user_identifier)
    attachment: MessageAttachment | None = None
    try:
        try:
            attachment = store_screened_image(
                content, declared_mime, "whatsapp-image", session, AttachmentDirection.INBOUND, db
            )
        except HTTPException:
            raise RuntimeError("image storage failed") from None  # transient: retry the event

        if silent:
            return await _store_image_without_ai(
                db, session, attachment, caption, paused, _fanout_customer_message, Message
            )

        request = MessageRequest(
            message=caption or IMAGE_ONLY_PLACEHOLDER,
            session_id=session.id,
            tenant_id=conn.tenant_id,
            user_identifier=user_identifier,
            attachment_ids=[attachment.id],
            metadata={"visitor_token": visitor.public_token, "entry_point": "whatsapp"},
        )
        channel = message_channel.set("whatsapp")
        try:
            response = await send_message(request, db)
        except HTTPException as exc:
            if exc.status_code == 402:
                _discard_unlinked(db, attachment)
                return await _plan_limit(
                    db, conn, chat, caption or IMAGE_ONLY_PLACEHOLDER, user_identifier
                )
            if 400 <= exc.status_code < 500:  # permanent: retrying re-runs moderation + upload
                _discard_unlinked(db, attachment)
                return await _unreadable(IMAGE_UNREADABLE_REPLY)
            raise
        finally:
            message_channel.reset(channel)
        chat.session_id = response.session_id
        db.commit()
        if response.message.role == MessageRole.USER:
            return "stored_human_owns_session"
        content_out = (response.message.content or "").strip()
        if content_out:
            await _enqueue_reply(conn, chat, content_out, f"ai:{response.message.id}")
        return "ai_reply"
    except Exception:
        _discard_unlinked(db, attachment)
        raise


async def _store_image_without_ai(
    db: Session,
    session: ConversationSession,
    attachment: MessageAttachment,
    caption: str,
    paused: bool,
    fanout,  # type: ignore[no-untyped-def]
    message_model,  # type: ignore[no-untyped-def]
) -> str:
    """Owner pause / AI off / a human owns it: persist the image message and show agents."""
    channel = message_channel.set("whatsapp")
    try:
        service = ConversationService(db)
        msg = service.add_message(
            session_id=session.id,
            role=MessageRole.USER,
            content=caption or IMAGE_ONLY_PLACEHOLDER,
        )
        service.link_attachments(session.id, msg.id, [attachment.id])
        db.commit()
    finally:
        message_channel.reset(channel)
    await fanout(db, session.id, select(message_model).where(message_model.id == msg.id))
    return "stored_image_owner_paused" if paused else "stored_image_human_owns"


async def _store_without_ai(
    db: Session,
    conn: WhatsAppConnection,
    chat: WhatsAppChat,
    text: str,
    user_identifier: str,
    paused: bool,
    fanout,  # type: ignore[no-untyped-def]
    message_model,  # type: ignore[no-untyped-def]
) -> str:
    """Owner pause / AI disabled: persist the customer's message and show it to agents."""
    if _plan_blocks_conversation(db, conn, new_session=_usable_session(db, conn, chat) is None):
        return await _plan_limit(db, conn, chat, text, user_identifier)
    session = _ensure_session(db, conn, chat, user_identifier)
    channel = message_channel.set("whatsapp")
    try:
        msg = ConversationService(db).add_message(
            session_id=session.id, role=MessageRole.USER, content=text
        )
    finally:
        message_channel.reset(channel)
    await fanout(db, session.id, select(message_model).where(message_model.id == msg.id))
    return "stored_owner_paused" if paused else "stored_ai_disabled"


async def _store_non_text(
    db: Session,
    conn: WhatsAppConnection,
    chat: WhatsAppChat,
    user_identifier: str,
    placeholder: str,
    reply: str | None,
    fanout,  # type: ignore[no-untyped-def]
    message_model,  # type: ignore[no-untyped-def]
) -> None:
    """Persist what happened to a message Luran could not read so the transcript is complete: a
    stand-in for the customer's media, plus the fixed reply when one was sent. Agents are shown
    the customer side.
    """
    session = _ensure_session(db, conn, chat, user_identifier)
    channel = message_channel.set("whatsapp")
    try:
        service = ConversationService(db)
        msg = service.add_message(session_id=session.id, role=MessageRole.USER, content=placeholder)
        if reply is not None:
            service.add_message(
                session_id=session.id,
                role=MessageRole.ASSISTANT,
                content=reply,
                meta={"source": "whatsapp_media_fallback"},
            )
    finally:
        message_channel.reset(channel)
    await fanout(db, session.id, select(message_model).where(message_model.id == msg.id))


async def _plan_limit(
    db: Session, conn: WhatsAppConnection, chat: WhatsAppChat, text: str, user_identifier: str
) -> str:
    """HTTP 402 from the pipeline (conversation limit / trial / subscription): store the
    message when a conversation exists, say NOTHING to the customer, alert the tenant."""
    session = _usable_session(db, conn, chat)
    if session is not None:
        ConversationService(db).add_message(
            session_id=session.id, role=MessageRole.USER, content=text
        )
    now = utcnow_aware()
    last_alert = ensure_utc_aware(conn.last_alert_at)
    if last_alert is None or now - last_alert > PLAN_ALERT_DEBOUNCE:
        conn.last_alert_at = now
        db.commit()
        # TODO(beta): email tenant admins that WhatsApp messages are blocked by their plan.
        logger.warning("whatsapp plan limit reached for tenant {}", conn.tenant_id)
    return "plan_limit"


# --------------------------------------------------------------------------- owner typed
async def handle_outbound_external(db: Session, conn: WhatsAppConnection, event: dict) -> str:
    """The owner typed from the phone / WhatsApp Web: pause the AI on that chat.

    Privacy: when this contact has never messaged the AI we only create a minimal chat row
    to hold the pause (no visitor, no conversation, no text stored) so personal messages the
    owner sends from the same number are not copied into Luran.
    """
    from app.core.websocket import connection_manager

    if not _tenant_may_use(db, conn.tenant_id):
        return "dropped_disabled"
    message_id = event["message_id"]
    if not _record_message_id(db, conn, message_id):
        return "duplicate"
    db.commit()
    try:
        now = utcnow_aware()
        chat, created = _get_or_create_chat(db, conn, event["to_ref"])
        chat.human_until = now + timedelta(minutes=settings.WHATSAPP_OWNER_PAUSE_MINUTES)
        db.commit()
        if created or chat.last_inbound_at is None:
            # The contact never wrote to the AI (also on the owner's 2nd, 3rd... message to them):
            # keep only the pause, never the text.
            return "paused_no_transcript"

        text = (event.get("text") or "").strip()
        if not text:
            return "paused_empty"
        session = _ensure_session(db, conn, chat, f"wa_{chat.contact_hmac[:24]}")
        msg = ConversationService(db).add_message(
            session_id=session.id,
            role=MessageRole.HUMAN_AGENT,
            content=text,
            meta={"source": "owner_phone"},
        )
        # Agents only: to_customer=True would echo the owner's own text back to WhatsApp.
        await connection_manager.fanout_event(
            session_id=session.id,
            message={
                "type": "agent_message",
                "message_id": str(msg.id),
                "content": text,
                "agent_name": "Owner (phone)",
                "timestamp": msg.created_at.isoformat(),
                "attachments": [],
                "reply_to": None,
            },
            to_customer=False,
            to_agents=True,
        )
        return "paused_stored"
    except Exception:
        _forget_message_id(db, conn, message_id)
        raise
