"""Live catalog lookup for one chat turn (spec 7.1-7.3). Postgres only, no model call.

`ground()` returns None when the tenant has nothing the AI should say about (no active items, or
the owner switched the catalog off): the caller then changes nothing, so such a tenant's prompt is
byte-identical to what it was before this feature. Otherwise it returns the block to inject plus
the facts the price guard and `meta.offerings` need. A failure after the gate fails closed.
"""

import logging
import time
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal
from collections.abc import Iterable
from uuid import UUID

from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from app.db.models.offering import (
    Offering,
    OfferingAvailability,
    OfferingImage,
    OfferingStatus,
    TenantOfferingsSettings,
)
from app.db.models.sales_agent_config import TenantSalesAgentConfig
from app.offerings.block import BLOCK_TOKEN_CAP, render_block, unavailable_block
from app.offerings.facts import OfferingFacts, facts_from_row
from app.offerings.prompt import channel_formatting
from app.services.rag_service import RAGService
from app.utils.datetime import ensure_utc_aware
from app.utils.token_counter import count_tokens

logger = logging.getLogger(__name__)

MENU_MAX = 12
DETAIL_MAX = 5
MAX_QUERY_TOKENS = 8

Mode = Literal["browse", "detail", "none", "unavailable"]

_CATALOG_FILLER = {
    "want",
    "buy",
    "need",
    "price",
    "prices",
    "much",
    "do",
    "does",
    "have",
    "got",
    "sell",
    "please",
    "pls",
    "plz",
    "can",
    "could",
    "would",
    "will",
    "there",
    "any",
    "some",
    "about",
    "me",
    "my",
    "we",
    "us",
    "am",
    "im",
    "get",
    "give",
    "show",
    "tell",
    "know",
    "like",
    "also",
    "this",
    "that",
    "those",
    "these",
    "them",
    "one",
    "ones",
    "first",
    "second",
    "third",
    "last",
    "other",
    "same",
    "cost",
    "costs",
    "how",
    "abeg",
    "wetin",
    "na",
    "ur",
    "hi",
    "hello",
    "hey",
    "thanks",
    "thank",
    "ok",
    "okay",
    "yes",
    "no",
}
_STOPWORDS = frozenset(RAGService.STOPWORDS) | _CATALOG_FILLER

_BROWSE = re.compile(
    r"\b(what\s+(do|have)\s+you\s+(have|got|sell|offer)|what\s+(are|is)\s+(your|the)\s+"
    r"(products?|services?|items?|offers?|prices?|menu|catalog(ue)?)|what\s+do\s+you\s+sell|"
    r"what('s| is)\s+available|catalog(ue)?|price\s*list|menu|everything\s+you|"
    r"your\s+(products?|services?|items?|offerings?)|show\s+me\s+(all|everything|what)|"
    r"what\s+services|list\s+(of\s+)?(your\s+)?(products?|services?|items?)|"
    r"anything\s+(available|else)|what\s+else)\b",
    re.IGNORECASE,
)
_ANAPHORA = re.compile(
    r"\b(it|that|this|those|these|them|the\s+(first|second|third|last|other|same)\s+one|"
    r"(first|second|third|last)\s+one)\b",
    re.IGNORECASE,
)


@dataclass
class GroundingResult:
    mode: Mode
    block: str
    items: list[OfferingFacts] = field(default_factory=list)  # every item named in the block
    matched_seqs: list[int] = field(default_factory=list)  # items shown in full
    total_active: int = 0
    currency: str = "NGN"
    read_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    strict: bool = False  # lookup failed: the guard treats any amount as a violation
    catalog_names: list[str] = field(default_factory=list)  # every active item's name
    token_count: int = 0

    @property
    def allowed_amounts(self) -> frozenset[int]:
        return frozenset(i.price for i in self.items if i.price is not None)

    @property
    def injected_refs(self) -> frozenset[str]:
        return frozenset(i.ref for i in self.items)

    @property
    def has_matches(self) -> bool:
        return bool(self.matched_seqs)

    @property
    def grounded(self) -> bool:
        """The catalog gave the model something to answer from (a hit, or a browse listing)."""
        return self.mode in ("browse", "detail")


# -- query building ---------------------------------------------------------------------------


def singular(token: str) -> str:
    if len(token) > 4 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 4 and token.endswith(("ches", "shes", "sses", "xes")):
        return token[:-2]
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def tokenize(text: str | None) -> list[str]:
    seen: dict[str, None] = {}
    for raw in re.findall(r"[a-z0-9]+", (text or "").lower()):
        if len(raw) < 2 or raw in _STOPWORDS:
            continue
        seen.setdefault(singular(raw), None)
    return list(seen)[:MAX_QUERY_TOKENS]


def is_browse_intent(text: str | None) -> bool:
    return bool(_BROWSE.search(text or ""))


def build_query_tokens(message: str, previous_message: str | None) -> list[str]:
    """Current message, plus the previous customer message when this one is short or anaphoric."""
    tokens = tokenize(message)
    if previous_message and (len(tokens) < 2 or _ANAPHORA.search(message or "")):
        for tok in tokenize(previous_message):
            if tok not in tokens:
                tokens.append(tok)
    return tokens[:MAX_QUERY_TOKENS]


# -- SQL ------------------------------------------------------------------------------------


def _word_prefix(column, token: str):
    col = func.lower(func.coalesce(column, ""))
    return or_(col.like(f"{token}%"), col.like(f"% {token}%"))


def _score_expr(tokens: list[str]):
    weighted = (
        (Offering.name, 3),
        (Offering.keywords, 3),
        (Offering.category, 2),
        (Offering.description, 1),
    )
    terms = [
        case((_word_prefix(col, tok), weight), else_=0)
        for tok in tokens
        for col, weight in weighted
    ]
    expr = terms[0]
    for term in terms[1:]:
        expr = expr + term
    return expr


def _active(tenant_id: UUID):
    return (Offering.tenant_id == tenant_id, Offering.status == OfferingStatus.ACTIVE)


def _search(db: Session, tenant_id: UUID, tokens: list[str], limit: int) -> list[Offering]:
    if not tokens:
        return []
    score = _score_expr(tokens)
    stmt = (
        select(Offering)
        .where(*_active(tenant_id), score > 0)
        .order_by(score.desc(), Offering.updated_at.desc(), Offering.seq)
        .limit(limit)
    )
    return list(db.execute(stmt).scalars())


def _by_seqs(db: Session, tenant_id: UUID, seqs: Iterable[int]) -> list[Offering]:
    wanted = list(dict.fromkeys(int(s) for s in seqs))
    if not wanted:
        return []
    rows = db.execute(
        select(Offering).where(*_active(tenant_id), Offering.seq.in_(wanted))
    ).scalars()
    by_seq = {r.seq: r for r in rows}
    return [by_seq[s] for s in wanted if s in by_seq]


def _menu_rows(db: Session, tenant_id: UUID, limit: int, exclude: set[UUID]) -> list[Offering]:
    sold_out_last = case((Offering.availability == OfferingAvailability.OUT_OF_STOCK, 1), else_=0)
    stmt = (
        select(Offering)
        .where(*_active(tenant_id))
        .order_by(sold_out_last, Offering.seq)
        .limit(limit + len(exclude))
    )
    return [r for r in db.execute(stmt).scalars() if r.id not in exclude][:limit]


def _count_active(db: Session, tenant_id: UUID) -> int:
    return int(
        db.execute(
            select(func.count()).select_from(Offering).where(*_active(tenant_id))
        ).scalar_one()
    )


def _all_names(db: Session, tenant_id: UUID) -> list[str]:
    """Every active item's name: the guard must know items that are not in this turn's block."""
    return [str(n) for (n,) in db.execute(select(Offering.name).where(*_active(tenant_id)))]


def _categories(db: Session, tenant_id: UUID) -> list[tuple[str, int]]:
    rows = db.execute(
        select(func.coalesce(Offering.category, "Other"), func.count())
        .where(*_active(tenant_id))
        .group_by(func.coalesce(Offering.category, "Other"))
        .order_by(func.count().desc(), func.coalesce(Offering.category, "Other"))
        .limit(8)
    ).all()
    return [(str(name), int(n)) for name, n in rows]


def _settings(db: Session, tenant_id: UUID) -> tuple[bool, str, datetime | None, int]:
    row = db.execute(
        select(TenantOfferingsSettings).where(TenantOfferingsSettings.tenant_id == tenant_id)
    ).scalar_one_or_none()
    if row is not None:
        return (
            bool(row.enabled),
            row.currency or "NGN",
            ensure_utc_aware(row.confirmed_at),
            int(row.stale_after_days),
        )
    currency = db.execute(
        select(TenantSalesAgentConfig.currency).where(TenantSalesAgentConfig.tenant_id == tenant_id)
    ).scalar_one_or_none()
    return True, currency or "NGN", None, 7


def attach_photos(db: Session, tenant_id: UUID, facts: list[OfferingFacts]) -> list[OfferingFacts]:
    """Photo count and cover for each item, in one query."""
    if not facts:
        return facts
    rows = db.execute(
        select(
            OfferingImage.offering_id, OfferingImage.id, OfferingImage.url, OfferingImage.position
        )
        .where(
            OfferingImage.tenant_id == tenant_id,
            OfferingImage.offering_id.in_([f.id for f in facts]),
        )
        .order_by(OfferingImage.offering_id, OfferingImage.position)
    ).all()
    counts: dict[UUID, int] = {}
    covers: dict[UUID, tuple[UUID, str]] = {}
    for offering_id, image_id, url, _pos in rows:
        counts[offering_id] = counts.get(offering_id, 0) + 1
        covers.setdefault(offering_id, (image_id, url))
    return [f.with_photos(counts.get(f.id, 0), *(covers.get(f.id) or (None, None))) for f in facts]


# -- the turn ---------------------------------------------------------------------------------


_SEEN_ITEMS: dict[UUID, float] = {}  # tenant -> when the gate last saw active items
_SEEN_TTL = 900.0
_SEEN_MAX = 2000


def _remember_items(tenant_id: UUID, has_items: bool) -> None:
    """Which tenants the gate recently saw with items: only they fail closed if the gate breaks."""
    if has_items:
        if len(_SEEN_ITEMS) >= _SEEN_MAX:
            _SEEN_ITEMS.pop(next(iter(_SEEN_ITEMS)), None)
        _SEEN_ITEMS[tenant_id] = time.monotonic()
    else:
        _SEEN_ITEMS.pop(tenant_id, None)


def _known_to_have_items(tenant_id: UUID) -> bool:
    seen = _SEEN_ITEMS.get(tenant_id)
    return seen is not None and time.monotonic() - seen < _SEEN_TTL


def ground(
    db: Session,
    tenant_id: UUID,
    message: str,
    *,
    previous_message: str | None = None,
    shown_seqs: Iterable[int] = (),
    channel: str | None = None,
    now: datetime | None = None,
) -> GroundingResult | None:
    now = now or datetime.now(UTC)
    try:
        # one indexed LIMIT 1 query: this is all a tenant without a catalog ever costs per turn
        has_items = db.execute(select(Offering.id).where(*_active(tenant_id)).limit(1)).first()
    except Exception:  # noqa: BLE001 - cannot tell whether this tenant has a catalog
        logger.exception("offerings gate query failed")
        db.rollback()  # the failed statement must not leave the transaction aborted
        if not _known_to_have_items(tenant_id):
            return None  # a tenant never seen with items is not told "the catalog is unavailable"
        block = unavailable_block(now)
        return GroundingResult(
            mode="unavailable",
            block=block,
            read_at=now,
            strict=True,
            token_count=count_tokens(block),
        )
    _remember_items(tenant_id, has_items is not None)
    if has_items is None:
        return None

    try:
        with db.begin_nested():
            enabled, currency, confirmed_at, stale_days = _settings(db, tenant_id)
            if not enabled:
                return None
            total = _count_active(db, tenant_id)
            return _ground(
                db,
                tenant_id,
                message,
                previous_message,
                list(shown_seqs),
                now,
                total,
                currency,
                confirmed_at,
                stale_days,
                channel,
            )
    except Exception:  # noqa: BLE001 - fail closed: say nothing about prices
        logger.exception("offerings lookup failed; failing closed")
        block = unavailable_block(now)
        return GroundingResult(
            mode="unavailable",
            block=block,
            total_active=0,
            read_at=now,
            strict=True,
            token_count=count_tokens(block),
        )


def _ground(
    db,
    tenant_id,
    message,
    previous_message,
    shown_seqs,
    now,
    total,
    currency,
    confirmed_at,
    stale_days,
    channel,
) -> GroundingResult:
    browse = is_browse_intent(message)
    tokens = build_query_tokens(message, previous_message)
    matched = [] if browse else _search(db, tenant_id, tokens, DETAIL_MAX)
    detail_rows = list(matched)
    follow_up = len(tokens) < 2 or bool(_ANAPHORA.search(message or ""))
    if not browse and follow_up:  # "the second one": a policy question is not about the items
        have = {r.id for r in detail_rows}
        for row in _by_seqs(db, tenant_id, shown_seqs):
            if row.id not in have and len(detail_rows) < DETAIL_MAX:
                detail_rows.append(row)
    if matched:
        mode: Mode = "detail"
    elif detail_rows:
        mode = "detail"  # a follow-up about what the customer was just shown
    elif browse:
        mode = "browse"
    else:
        mode = "none"

    show_menu = browse or total <= MENU_MAX
    menu_rows = (
        _menu_rows(db, tenant_id, MENU_MAX, {r.id for r in detail_rows}) if show_menu else []
    )

    detail_facts = attach_photos(db, tenant_id, [facts_from_row(r) for r in detail_rows])
    menu_facts = [facts_from_row(r) for r in menu_rows]
    rendered = render_block(
        detail=detail_facts,
        menu=menu_facts,
        total_active=total,
        categories=_categories(db, tenant_id),
        now=now,
        confirmed_at=confirmed_at,
        stale_after_days=stale_days,
        currency=currency,
        nothing_matched=(mode == "none"),
        formatting=channel_formatting(channel),
    )
    return GroundingResult(
        mode=mode,
        block=rendered.text,
        items=rendered.items,
        matched_seqs=[i.seq for i in rendered.detail],
        total_active=total,
        currency=currency,
        read_at=now,
        token_count=count_tokens(rendered.text),
        catalog_names=_all_names(db, tenant_id),
    )


__all__ = ["BLOCK_TOKEN_CAP", "GroundingResult", "ground", "build_query_tokens", "is_browse_intent"]
