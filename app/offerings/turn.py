"""What `send_message` needs from the catalog for one turn, kept out of `chat.py`."""

import logging
from datetime import datetime
from uuid import UUID

from sqlalchemy.orm import Session

from app.db.models.conversation import ConversationSession
from app.offerings.facts import facts_from_row
from app.offerings.grounding import GroundingResult, attach_photos, ground
from app.offerings.prompt import catalog_sales_addendum
from app.offerings.writer import card, shown_items
from app.services.conversation_service import message_channel, voice_channel

logger = logging.getLogger(__name__)

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
    """What the customer has written in this conversation (they may quote it back): the last few
    messages, each bounded, so an adversarial wall of digits costs a bounded amount of work."""
    from app.offerings.bargain import cap

    texts = [
        m.content
        for m in history
        if str(getattr(m.role, "value", m.role)).lower() == "user" and m.content
    ]
    return cap([*texts, current])


def context_prices(db: Session, tenant_id: UUID, session_meta: dict | None, session_id) -> list:
    """Prices of the items already in play in this conversation (shown, or wanted on its lead), for
    a figure-only message that matched nothing by itself ("I fit pay 18k")."""
    seqs = [*shown_seqs(session_meta), *wanted_seqs(db, session_id)]
    if not seqs:
        return []
    from app.offerings.grounding import _by_seqs

    return [r.price for r in _by_seqs(db, tenant_id, seqs[-10:])]


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


def build_offerings_turn(
    db: Session,
    tenant_id: UUID,
    result: GroundingResult,
    guard: dict | None,
    reply: str,
    sales_meta: dict | None,
    token_seqs: list[int] | None = None,
) -> tuple[dict, list[int]]:
    """`message.meta.offerings` (frozen contract) and the seqs this reply showed."""
    guard = guard or {"checked": False, "blocked": False, "regenerated": False, "fell_back": False}
    items = shown_items(
        result,
        reply,
        sales_signals=(sales_meta or {}).get("signals") if sales_meta is not None else None,
        fell_back=bool(guard.get("fell_back")),
        token_seqs=token_seqs or [],
    )
    try:
        items = attach_photos(db, tenant_id, items)
    except Exception:  # noqa: BLE001 - cards without photos beat no reply
        logger.exception("offerings card photos failed (non-fatal)")
    meta = {
        "mode": result.mode if result.mode in ("browse", "detail") else "none",
        "items": [card(i, result.currency) for i in items],
        "shown_at": result.read_at.isoformat(),
        "guard": guard,
    }
    return meta, [i.seq for i in items]


OFFER_WATCH_KEY = "offer_watch"
OFFER_WATCH_TURNS = 5  # replies after a bargaining turn that still answer under the offer rules


def offer_watch(session_meta: dict | None) -> int:
    raw = (session_meta or {}).get(OFFER_WATCH_KEY)
    return raw if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0 else 0


def arms_countdown(message: str, grounding, signal: bool) -> bool:
    """Only an offer that holds a figure (typed by the customer, or returned by the model) keeps the
    agreement rules armed afterwards: "is the price negotiable?" is answered but arms nothing."""
    from app.offerings.amounts import customer_amounts
    from app.offerings.bargain import cap

    if signal:
        return True
    names = grounding.name_index()
    text = (cap([message]) or [""])[0]
    return bool(customer_amounts(text, small_ok=True, names=names))


def next_offer_watch(current: int, offer_turn: bool) -> int:
    """Re-armed by a bargaining turn, then runs down one reply at a time."""
    return OFFER_WATCH_TURNS if offer_turn else max(current - 1, 0)


def remember_shown(
    db: Session, session_id: UUID, seqs: list[int], *, armed: bool | None = None
) -> None:
    """`session_meta["offerings_shown"]`: the last five refs shown, so "the second one" resolves;
    `armed` (catalog tenants only; None for everyone else) updates the offer-in-play countdown from
    the value read under the row lock, so two concurrent messages cannot overwrite each other's
    count: an offer arms it, any other turn runs it down."""
    if not seqs and armed is None:
        return
    try:
        from sqlalchemy import select

        # the row lock keeps two concurrent messages (WhatsApp) from losing each other's write
        row = db.execute(
            select(ConversationSession)
            .where(ConversationSession.id == session_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()
        if row is None:
            return
        meta = dict(row.meta or {})
        changed = False
        if seqs:
            merged = [s for s in shown_seqs(meta) if s not in seqs] + seqs
            meta[SHOWN_KEY] = merged[-5:]
            changed = True
        if armed is not None:
            watch = next_offer_watch(offer_watch(meta), armed)
            if watch != offer_watch(meta):
                meta[OFFER_WATCH_KEY] = watch
                changed = True
        if changed:
            row.meta = meta
            db.commit()
    except Exception:  # noqa: BLE001
        logger.exception("offerings shown-memory write failed (non-fatal)")
        db.rollback()


def lead_source(channel: str | None, visitor) -> str:
    """Lead `source` from where the conversation happens (was hard-coded `chat_widget`)."""
    if channel in ("voice", "whatsapp"):
        return channel
    from app.db.models.visitor import VisitorSource

    if getattr(visitor, "created_from", None) == VisitorSource.SUPPORT_PAGE:
        return "hosted_page"
    return "chat_widget"


def customer_figures(texts: list[str]) -> set[int]:
    """Whole-naira figures the customer wrote (so a recorded offer is provably theirs)."""
    from app.offerings.amounts import customer_amounts

    out: set[int] = set()
    for text in texts:
        for a in customer_amounts(text, small_ok=True):
            if a.value == a.value.to_integral_value():
                out.add(int(a.value))
    return out


def wanted_seqs(db: Session, session_id: UUID) -> list[int]:
    """Items already wanted on this conversation's lead: a later cart may keep naming them."""
    from sqlalchemy import select

    from app.db.models.lead import Lead
    from app.db.models.offering import LeadOfferingInterest

    rows = db.execute(
        select(LeadOfferingInterest.ref)
        .join(Lead, Lead.id == LeadOfferingInterest.lead_id)
        .where(Lead.conversation_id == session_id)
    ).all()
    return [int(r[0].lstrip("#")) for r in rows if str(r[0]).lstrip("#").isdigit()]


def safe_offerings_turn(
    db: Session,
    tenant_id: UUID,
    result: GroundingResult,
    guard: dict | None,
    reply: str,
    sales_meta: dict | None,
    token_seqs: list[int] | None = None,
) -> tuple[dict, list[int]]:
    """`build_offerings_turn`, but a failure yields card-less meta instead of an error reply."""
    try:
        return build_offerings_turn(db, tenant_id, result, guard, reply, sales_meta, token_seqs)
    except Exception:  # noqa: BLE001
        logger.exception("offerings cards failed (non-fatal)")
        db.rollback()
        return (
            {
                "mode": "none",
                "items": [],
                "shown_at": result.read_at.isoformat(),
                "guard": guard
                or {"checked": False, "blocked": False, "regenerated": False, "fell_back": False},
            },
            [],
        )


def offered_signal(sales_meta: dict | None) -> bool:
    return bool(((sales_meta or {}).get("signals") or {}).get("offered_price"))


def strip_token_text(text: str, *, brackets: bool = False) -> str:
    """Prompt text with exact tokens and bracketed look-alikes removed (catalog tenants only);
    byte-identical when it has none. `brackets=True` also drops any `[[...]]` (KB text placed in a
    catalog prompt: the model is told to write tokens, so a wiki-style double bracket must not be
    copied into a reply)."""
    from app.offerings import markup

    return markup.strip_brackets(text) if brackets else markup.strip_price_markup(text)


def finalize_reply(
    db: Session, tenant_id: UUID, result: GroundingResult | None, reply: str
) -> tuple[str, list[int]]:
    """Resolve price tokens to the live price text of their own row (never a token on the wire).

    A tenant without a catalog block (result None) gets its reply back untouched: no scan at all.
    A token that cannot be resolved (the item vanished since the block) gives the code fallback.
    Returns the final text and the item seqs the tokens named.
    """
    from app.offerings import tokens
    from app.offerings.fallback import fallback_reply
    from app.offerings.grounding import _by_seqs

    if result is None:
        return reply, []  # no catalog for this tenant: the reply is never touched, not even read
    if not tokens.has_markup(reply):
        return reply, []  # byte-identical: an ordinary reply is never touched
    seqs = tokens.token_seqs(reply)
    live = {
        row.seq: facts_from_row(row).price_text(result.currency)
        for row in _by_seqs(db, tenant_id, seqs)
    }
    resolved = tokens.resolve(reply, live.get)
    if resolved is not None:
        resolved = tokens.collapse_mode_words(resolved)
    final = fallback_reply(result) if resolved is None else resolved
    if tokens.has_markup(final):  # last scan: tenant text or a price note cannot smuggle one out
        final = tokens.strip(final)
        if tokens.has_markup(final):  # still there (an entity-disguised one): the code reply
            final = tokens.strip(fallback_reply(result))
    return final, seqs
