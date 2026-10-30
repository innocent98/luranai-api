"""What `send_message` needs from the catalog for one turn, kept out of `chat.py`."""

from datetime import datetime
from uuid import UUID

from sqlalchemy.orm import Session

from app.offerings.grounding import GroundingResult, ground
from app.offerings.prompt import catalog_sales_addendum
from app.services.conversation_service import message_channel, voice_channel

SHOWN_KEY = "offerings_shown"


def turn_channel() -> str | None:
    """`voice`, `whatsapp`, or None for the web widget / hosted page."""
    if voice_channel.get():
        return "voice"
    return message_channel.get()


def previous_customer_message(history: list, current: str) -> str | None:
    users = [
        m.content
        for m in history
        if str(getattr(m.role, "value", m.role)).lower() == "user" and m.content
    ]
    if users and users[-1] == current:
        users = users[:-1]
    return users[-1] if users else None


def shown_seqs(session_meta: dict | None) -> list[int]:
    raw = (session_meta or {}).get(SHOWN_KEY)
    if not isinstance(raw, list):
        return []
    return [s for s in raw if isinstance(s, int) and not isinstance(s, bool) and s > 0][-5:]


def ground_turn(
    db: Session,
    tenant_id: UUID,
    message: str,
    *,
    history: list,
    session_meta: dict | None,
    now: datetime | None = None,
) -> GroundingResult | None:
    return ground(
        db,
        tenant_id,
        message,
        previous_message=previous_customer_message(history, message),
        shown_seqs=shown_seqs(session_meta),
        channel=turn_channel(),
        now=now,
    )


def sales_kwargs(result: GroundingResult | None) -> dict:
    """Extra `generate_sales_response` kwargs; empty for an ungrounded or fail-closed turn."""
    if result is None or result.mode == "unavailable":
        return {}
    return {"catalog_addendum": catalog_sales_addendum(turn_channel())}
