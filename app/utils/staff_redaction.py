"""Strip customer-only secrets from message meta before it reaches staff (spec §4.6)."""

from app.schemas.conversation import MessageResponse

STAFF_REDACTED_META_KEYS = frozenset({"rating_token"})


def redact_staff_meta(meta: dict | None) -> dict | None:
    if not meta or not any(key in meta for key in STAFF_REDACTED_META_KEYS):
        return meta
    return {k: v for k, v in meta.items() if k not in STAFF_REDACTED_META_KEYS}


def redact_staff_messages(messages: list[MessageResponse]) -> list[MessageResponse]:
    return [m.model_copy(update={"meta": redact_staff_meta(m.meta)}) for m in messages]
