"""N2 reply-to: validate the quoted message and freeze the snapshot taken at send time.

The snapshot (sender type, kind, <=200-char snippet) is stored on the reply so the quote
survives attachment retention and originals outside the loaded window. It carries no
sender identity beyond the coarse type, so the same payload is safe on customer and
staff surfaces.
"""

from dataclasses import dataclass
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.conversation import (
    AttachmentKind,
    ConversationSession,
    Message,
    MessageAttachment,
    MessageRole,
    SessionStatus,
)

SNIPPET_MAX_CHARS = 200
# Chat image/video-only sends persist these placeholders as message text; the quote shows
# the kind label instead, so the snippet is blank.
_PLACEHOLDER_TEXTS = frozenset({"[image]", "[screen recording]"})
_SENDER_TYPES = {
    MessageRole.USER: "customer",
    MessageRole.ASSISTANT: "ai",
    MessageRole.HUMAN_AGENT: "agent",
}

NOT_FOUND = "REPLY_TARGET_NOT_FOUND"
NOT_QUOTABLE = "REPLY_TARGET_NOT_QUOTABLE"
SESSION_CLOSED = "REPLY_SESSION_CLOSED"


class ReplyToError(Exception):
    """A reply was refused; `code` is a stable contract for the clients."""

    def __init__(self, code: str, message: str, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code

    def detail(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True)
class ReplySnapshot:
    message_id: UUID
    snapshot: dict[str, str]


def is_quotable(message: Message) -> bool:
    """System notices and voice transcript lines cannot be quoted (spec N2-D2)."""
    if message.role == MessageRole.SYSTEM:
        return False
    meta = message.meta or {}
    return not meta.get("system_notice") and meta.get("channel") != "voice"


def _kind(db: Session, message_id: UUID) -> str:
    kinds = set(
        db.execute(select(MessageAttachment.kind).where(MessageAttachment.message_id == message_id))
        .scalars()
        .all()
    )
    if AttachmentKind.VIDEO in kinds:
        return "video"
    if AttachmentKind.IMAGE in kinds:
        return "image"
    return "text"


def _snippet(content: str) -> str:
    text = (content or "").strip()
    if text in _PLACEHOLDER_TEXTS:
        return ""
    if len(text) <= SNIPPET_MAX_CHARS:
        return text
    return text[: SNIPPET_MAX_CHARS - 1] + "…"


def resolve_reply_to(
    db: Session, session: ConversationSession, reply_to_message_id: UUID
) -> ReplySnapshot:
    """Validate the quote target and build its snapshot. Nothing is persisted."""
    if session.status == SessionStatus.RESOLVED:
        raise ReplyToError(SESSION_CLOSED, "This conversation is closed; replies are disabled", 409)

    target = db.get(Message, reply_to_message_id)
    # A message from another session is reported exactly like a missing one, so the
    # endpoint cannot be used to probe for message ids.
    if target is None or target.session_id != session.id:
        raise ReplyToError(NOT_FOUND, "The message you are replying to was not found", 422)
    if not is_quotable(target):
        raise ReplyToError(NOT_QUOTABLE, "This message cannot be replied to", 422)

    sender_type = _SENDER_TYPES.get(target.role)
    if sender_type is None:  # pragma: no cover - SYSTEM already excluded above
        raise ReplyToError(NOT_QUOTABLE, "This message cannot be replied to", 422)
    return ReplySnapshot(
        message_id=target.id,
        snapshot={
            "sender_type": sender_type,
            "kind": _kind(db, target.id),
            "snippet": _snippet(target.content),
        },
    )


def http_error(error: ReplyToError) -> HTTPException:
    return HTTPException(status_code=error.status_code, detail=error.detail())


def resolve_for_session_id(
    db: Session, session_id: UUID | None, reply_to_message_id: UUID
) -> ReplySnapshot:
    """`resolve_reply_to` for a caller that only has a session id (REST send paths)."""
    session = db.get(ConversationSession, session_id) if session_id else None
    if session is None:
        # A reply needs an existing conversation; there is nothing to quote in a new one.
        raise http_error(
            ReplyToError(NOT_FOUND, "The message you are replying to was not found", 422)
        )
    try:
        return resolve_reply_to(db, session, reply_to_message_id)
    except ReplyToError as exc:
        raise http_error(exc) from None


def frame_reply_to(reply_to) -> dict | None:  # noqa: ANN001 - ReplyToOut | None
    """WS/JSON form of a message's quote (`None` when it is not a reply)."""
    return reply_to.model_dump(mode="json") if reply_to is not None else None


_SPEAKER = {"customer": "their own", "agent": "a support agent's", "ai": "the assistant's"}


def quote_context(user_text: str, reply: ReplySnapshot | None) -> str:
    """Prefix the customer's text with the message they are replying to (AI prompt only).

    Quoted images are intentionally not re-sent to vision (spec N2-D5); the kind label
    stands in when the quote has no text.
    """
    if reply is None:
        return user_text
    quoted = reply.snapshot["snippet"] or f"[{reply.snapshot['kind']}]"
    speaker = _SPEAKER[reply.snapshot["sender_type"]]
    return f'[The customer is replying to {speaker} earlier message: "{quoted}"]\n\n{user_text}'
