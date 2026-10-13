"""Strip customer-only secrets before they reach staff (spec §4.6, staff credential hygiene).

Customer bearer credentials: the N4 `rating_token`, the visitor's `public_token`
(and the `visitor_token` / `visitor_session_token` copies the widget stores in
session meta) and the raw `user_identifier` (customer socket auth = session_id +
user_identifier). Staff get a non-secret `customer_label` instead.
"""

from uuid import UUID

from sqlalchemy.orm import Session

from app.schemas.conversation import MessageResponse

STAFF_REDACTED_META_KEYS = frozenset({"rating_token"})
STAFF_REDACTED_SESSION_META_KEYS = frozenset({"visitor_token", "visitor_session_token"})


def _without(meta: dict | None, keys: frozenset[str]) -> dict | None:
    if not meta or not any(key in meta for key in keys):
        return meta
    return {k: v for k, v in meta.items() if k not in keys}


def redact_staff_meta(meta: dict | None) -> dict | None:
    return _without(meta, STAFF_REDACTED_META_KEYS)


def redact_staff_session_meta(meta: dict | None) -> dict | None:
    return _without(meta, STAFF_REDACTED_META_KEYS | STAFF_REDACTED_SESSION_META_KEYS)


def redact_staff_messages(messages: list[MessageResponse]) -> list[MessageResponse]:
    return [m.model_copy(update={"meta": redact_staff_meta(m.meta)}) for m in messages]


def customer_label(
    session_id: UUID,
    *,
    name: str | None = None,
    email: str | None = None,
    phone: str | None = None,
) -> str:
    """Display label for a customer on staff surfaces; never derived from a credential."""
    return name or email or phone or f"Visitor {str(session_id)[:8]}"


def staff_customer_label(db: Session, session_id: UUID, visitor_id: UUID | None) -> str:
    """`customer_label` resolved from the session's linked visitor (one PK lookup)."""
    from app.db.models.visitor import Visitor

    visitor = db.get(Visitor, visitor_id) if visitor_id else None
    if visitor is None:
        return customer_label(session_id)
    return customer_label(session_id, name=visitor.name, email=visitor.email, phone=visitor.phone)
