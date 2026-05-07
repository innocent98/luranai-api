"""Helpers for normalizing conversation attribution fields (source, source_meta)."""
from __future__ import annotations

from app.schemas.explore import ConversationSource, SourceMeta


def coerce_source(raw: str | None) -> str:
    """Map a raw source string to a known ConversationSource value, defaulting to 'unknown'.

    Conversation creation must never fail because of an unknown source — the field is
    analytics-only. Older clients (embedded widget) may not know the latest enum values;
    silently coercing keeps them working.
    """
    if raw is None:
        return ConversationSource.UNKNOWN.value
    try:
        return ConversationSource(raw).value
    except ValueError:
        # Unknown value — coerce. (TODO: increment a counter if we want to detect drift.)
        return ConversationSource.UNKNOWN.value


def normalize_source_meta(raw: dict | SourceMeta | None) -> dict | None:
    """Normalize source_meta into a dict, dropping unknown keys via SourceMeta validation.

    Returns None when input is None. Otherwise validates with the SourceMeta model
    (which has extra="ignore") and returns the resulting dict.
    """
    if raw is None:
        return None
    if isinstance(raw, SourceMeta):
        return raw.model_dump(exclude_none=False)
    return SourceMeta.model_validate(raw).model_dump(exclude_none=False)
