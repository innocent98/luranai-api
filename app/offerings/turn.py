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


def customer_texts(history: list, current: str) -> list[str]:
    """Everything the customer has written in this conversation (they may be quoted back)."""
    texts = [
        m.content
        for m in history
        if str(getattr(m.role, "value", m.role)).lower() == "user" and m.content
    ]
    return [*texts, current]


def kb_text_from_prompt(prompt: str) -> str:
    """The knowledge-base section of the assembled prompt, i.e. what the model was shown."""
    start = prompt.find("Retrieved knowledge:")
    end = prompt.find("Conversation history:", start + 1) if start >= 0 else -1
    return prompt[start:end] if start >= 0 and end > start else ""


def tier_amounts(sales_config) -> list[int]:
    """Plan prices the sales directive injects when the tenant has pricing tiers."""
    out: list[int] = []
    for tier in getattr(sales_config, "pricing_tiers", None) or []:
        if not isinstance(tier, dict):
            continue
        for key in ("base_price", "per_seat_overage_price"):
            try:
                value = int(tier.get(key) or 0)
            except (TypeError, ValueError):
                continue
            if value > 0:
                out.append(value)
    return out


def offerings_meta(result: GroundingResult, guard: dict | None) -> dict:
    """`message.meta.offerings` (frozen contract). Items are filled by the reply writer."""
    return {
        "mode": result.mode if result.mode in ("browse", "detail") else "none",
        "items": [],
        "shown_at": result.read_at.isoformat(),
        "guard": guard
        or {"checked": False, "blocked": False, "regenerated": False, "fell_back": False},
    }
