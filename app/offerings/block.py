"""Renders the authoritative catalog block (spec 7.3) under a hard token cap.

Truncation drops the lowest-ranked items first (detail items before menu lines, last first), so the
set of items that end up in the text is exactly the set the guard may treat as injected.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.offerings.facts import OfferingFacts, format_amount
from app.offerings.rules import OFFERINGS_RULES
from app.utils.token_counter import count_tokens

BLOCK_TOKEN_CAP = 1000


@dataclass
class RenderedBlock:
    text: str
    detail: list[OfferingFacts] = field(default_factory=list)
    menu: list[OfferingFacts] = field(default_factory=list)

    @property
    def items(self) -> list[OfferingFacts]:
        return [*self.detail, *self.menu]


def _fmt_time(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%H:%M")


def _fmt_date(dt: datetime) -> str:
    d = dt.astimezone(UTC)
    return f"{d.day} {d:%b %Y}"


def freshness_lines(
    now: datetime, confirmed_at: datetime | None, stale_after_days: int
) -> list[str]:
    lines = [f"Read live at {_fmt_time(now)} UTC on {_fmt_date(now)}."]
    if confirmed_at is None:
        lines.append("The owner has not confirmed this list yet.")
        return lines
    age_days = (now - confirmed_at).total_seconds() / 86400
    if confirmed_at.astimezone(UTC).date() == now.astimezone(UTC).date():
        lines.append(f"The owner last confirmed this list today at {_fmt_time(confirmed_at)} UTC.")
    else:
        lines.append(f"The owner last confirmed this list on {_fmt_date(confirmed_at)}.")
    if stale_after_days > 0 and age_days > stale_after_days:
        lines.append(
            f"This list is stale: once, say \"as of the owner's last update on "
            f'{_fmt_date(confirmed_at)}; please confirm with the team before paying".'
        )
    return lines


def _header(now, confirmed_at, stale_after_days, currency) -> str:
    return "\n".join(
        [
            "## Offerings catalog (authoritative: the only source of prices, stock and items)",
            *freshness_lines(now, confirmed_at, stale_after_days),
            f"Currency: {currency}. Amounts are whole units.",
            OFFERINGS_RULES,
        ]
    )


def detail_line(item: OfferingFacts, currency: str) -> str:
    parts = [
        item.name,
        f"kind: {item.kind.value}",
    ]
    if item.category:
        parts.append(f"category: {item.category}")
    parts.append(f"price: {item.price_text(currency)}")
    parts.append(f"availability: {item.availability_text(with_quantity=True)}")
    parts.append("negotiable: yes (owner open to offers)" if item.negotiable else "negotiable: no")
    parts.append(f"photos: {item.photo_count}")
    if item.description:
        parts.append(f"description: {item.description}")
    return f'<offering ref="{item.ref}">{" | ".join(parts)}</offering>'


def menu_line(item: OfferingFacts, currency: str) -> str:
    parts = [item.name, item.kind.value, item.price_text(currency), item.availability_text()]
    if item.negotiable:
        parts.append("negotiable")
    return f'<offering ref="{item.ref}">{" · ".join(parts)}</offering>'


def _category_summary(categories: list[tuple[str, int]], more: int) -> str:
    if more <= 0:
        return ""
    cats = ", ".join(f"{name} {count}" for name, count in categories[:8])
    return f"+{more} more items not shown. Categories: {cats}. Ask which category if unclear."


def render_block(
    *,
    detail: list[OfferingFacts],
    menu: list[OfferingFacts],
    total_active: int,
    categories: list[tuple[str, int]],
    now: datetime,
    confirmed_at: datetime | None,
    stale_after_days: int,
    currency: str,
    nothing_matched: bool,
) -> RenderedBlock:
    header = _header(now, confirmed_at, stale_after_days, currency)
    note = (
        "Nothing listed matches this message. Say it is not listed and offer to connect the "
        "customer with the team. Do not suggest or invent items."
        if nothing_matched
        else ""
    )

    def assemble(d: list[OfferingFacts], m: list[OfferingFacts]) -> str:
        sections = [header]
        if note:
            sections.append(note)
        if d:
            sections.append("Items in full:\n" + "\n".join(detail_line(i, currency) for i in d))
        if m:
            sections.append("Listed items:\n" + "\n".join(menu_line(i, currency) for i in m))
        summary = _category_summary(categories, total_active - len(d) - len(m))
        if summary:
            sections.append(summary)
        return "\n\n".join(sections)

    kept_detail: list[OfferingFacts] = []
    kept_menu: list[OfferingFacts] = []
    for item in detail:
        if count_tokens(assemble([*kept_detail, item], kept_menu)) > BLOCK_TOKEN_CAP:
            break
        kept_detail.append(item)
    for item in menu:
        if count_tokens(assemble(kept_detail, [*kept_menu, item])) > BLOCK_TOKEN_CAP:
            break
        kept_menu.append(item)
    return RenderedBlock(text=assemble(kept_detail, kept_menu), detail=kept_detail, menu=kept_menu)


def unavailable_block(now: datetime) -> str:
    return "\n".join(
        [
            "## Offerings catalog (authoritative)",
            f"Read at {_fmt_time(now)} UTC on {_fmt_date(now)}: the catalog is unavailable right now.",
            "Prices and availability are unavailable right now. State no price, no stock level "
            "and no total, and do not name or suggest items. Say you cannot check the list at the "
            "moment and offer to connect the customer with the team.",
        ]
    )


__all__ = ["BLOCK_TOKEN_CAP", "RenderedBlock", "render_block", "unavailable_block", "format_amount"]
