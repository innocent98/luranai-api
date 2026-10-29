"""WhatsApp contact exclusion: normalisation, matching and cleanup (hot path, no HTTP).

A listed contact is ignored entirely. Matching never needs the full number stored: entries hold
peppered HMACs (`app.channels.identity`). The sidecar's chat ref is often a LID, so an entry also
learns the LID's key the first time an inbound event carries both the LID and the contact's
phone digits (owner-typed events carry only the ref).
"""

from __future__ import annotations

import re
from uuid import UUID

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from app.channels import identity
from app.db.models.conversation import ConversationSession
from app.db.models.visitor import Visitor
from app.db.models.whatsapp import WhatsAppChat, WhatsAppContactExclusion

MAX_EXCLUSIONS_PER_CONNECTION = 5000
MAX_BULK_ENTRIES = 200
LABEL_MAX = 60

_PHONE_JID = re.compile(r"^([1-9][0-9]{7,14})@s\.whatsapp\.net$")
_SEPARATORS = re.compile(r"[\s\-.()]")
_TRUNK_ZERO = re.compile(r"\(0\)")  # "+44 (0)7700 900123": the (0) is not part of the number
# Country codes whose national numbers never start with 0 but are habitually written with the
# trunk prefix after the code ("+234 0803..."). WhatsApp uses the number without it.
_TRUNK_ZERO_CODES = ("234", "44", "33", "49", "61", "27")


def normalize_exclusion_phone(raw: str | None) -> str | None:
    """Digits with country code, no '+'. Tolerant of what a person types: spaces, dashes, dots,
    brackets, a leading '+' or '00'. A national format ('0801...') has no country code to resolve
    and is rejected (None), as is anything that is not 8-15 digits."""
    cleaned = _SEPARATORS.sub("", _TRUNK_ZERO.sub("", raw or ""))
    if cleaned.startswith("+"):
        cleaned = cleaned[1:]
    elif cleaned.startswith("00"):
        cleaned = cleaned[2:]
    for code in _TRUNK_ZERO_CODES:
        if cleaned.startswith(code + "0"):
            cleaned = code + cleaned[len(code) + 1 :]
            break
    return cleaned if identity.normalize_phone(cleaned) is not None else None


def digits_from_ref(ref: str | None) -> str | None:
    """The phone digits of a phone-JID chat ref (`<digits>@s.whatsapp.net`); None for a LID."""
    match = _PHONE_JID.fullmatch(ref or "")
    return match.group(1) if match else None


def phone_jid(digits: str) -> str:
    return f"{digits}@s.whatsapp.net"


def _candidate_phone_keys(ref: str | None, phone_digits: str | None) -> list[str]:
    digits: set[str] = set()
    ref_digits = digits_from_ref(ref)
    if ref_digits:
        digits.add(ref_digits)
    declared = identity.normalize_phone(phone_digits or "")
    if declared:
        digits.add(declared)
    return [identity.phone_hmac(d) for d in sorted(digits)]


def is_excluded(
    db: Session, connection_id: UUID, *, ref: str | None, phone_digits: str | None = None
) -> bool:
    """True when this contact is on the connection's list. Read-mostly: the one write is the LID
    learning (an update of the entry itself, plus the removal of a pause-only chat row), committed
    here. Raises `identity.PepperMissing` like every other WhatsApp lookup (the webhook ignores it
    and the worker retries; the sender turns it into a retry)."""
    if not ref and not phone_digits:
        return False
    phone_keys = _candidate_phone_keys(ref, phone_digits)
    ref_key = identity.contact_hmac(ref) if ref else None
    if ref_key:
        # An existing chat of this ref remembers the number once the sidecar resolved it: a
        # contact excluded AFTER they were first seen by LID is recognised without new digits.
        known = db.execute(
            select(WhatsAppChat.phone_hmac).where(
                WhatsAppChat.connection_id == connection_id,
                WhatsAppChat.contact_hmac == ref_key,
            )
        ).scalar_one_or_none()
        if known and known not in phone_keys:
            phone_keys.append(known)
    conditions = []
    if phone_keys:
        conditions.append(WhatsAppContactExclusion.phone_hmac.in_(phone_keys))
    if ref_key:
        conditions.append(WhatsAppContactExclusion.lid_hmac == ref_key)
    if not conditions:
        return False
    entry = db.execute(
        select(WhatsAppContactExclusion)
        .where(WhatsAppContactExclusion.connection_id == connection_id, or_(*conditions))
        .limit(1)
    ).scalar_one_or_none()
    if entry is None:
        return False
    # Learn the LID ref from a phone match (never from the ref alone, never for a phone JID).
    if (
        ref
        and ref_key
        and ref.endswith("@lid")
        and entry.lid_hmac != ref_key
        and entry.phone_hmac in phone_keys
    ):
        entry.lid_hmac = ref_key
        purge_sessionless_chats(db, connection_id, [ref_key])
        db.commit()
    return True


def chat_phone_hmac(ref: str | None, phone_digits: str | None) -> str | None:
    """The phone key to remember on a chat: from the sidecar's resolved digits, else the digits in
    a phone-JID ref. None when neither is usable."""
    digits = identity.normalize_phone(phone_digits or "") or digits_from_ref(ref)
    return identity.phone_hmac(digits) if digits else None


def purge_sessionless_chats(db: Session, connection_id: UUID, contact_hmacs: list[str]) -> int:
    """Delete chat rows (and the WhatsApp visitor they made) that never became a conversation:
    owner-pause rows and plan-limit leftovers. A chat with a conversation is history and stays."""
    if not contact_hmacs:
        return 0
    chats = list(
        db.execute(
            select(WhatsAppChat).where(
                WhatsAppChat.connection_id == connection_id,
                WhatsAppChat.contact_hmac.in_(contact_hmacs),
                WhatsAppChat.session_id.is_(None),
            )
        ).scalars()
    )
    visitor_ids = {c.visitor_id for c in chats if c.visitor_id}
    for chat in chats:
        db.delete(chat)
    db.flush()
    for visitor_id in visitor_ids:
        in_use = db.execute(
            select(ConversationSession.id).where(ConversationSession.visitor_id == visitor_id)
        ).first()
        other_chat = db.execute(
            select(WhatsAppChat.id).where(WhatsAppChat.visitor_id == visitor_id)
        ).first()
        if not in_use and not other_chat:
            db.execute(delete(Visitor).where(Visitor.id == visitor_id))
    return len(chats)
