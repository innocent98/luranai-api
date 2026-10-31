"""Plain-text rendering of a catalog quote for channels without cards (WhatsApp, voice)."""

import re

from app.offerings.facts import format_amount

_QTY = re.compile(r"^\s*(\d+)\s*×")
TEAM_LINE = "The team will confirm availability and arrange payment."


def render_quote_text(quote: dict) -> str:
    """Every figure comes from the quote row itself; nothing is computed here."""
    currency = quote.get("currency") or "NGN"
    lines = []
    for item in quote.get("line_items") or []:
        m = _QTY.match(str(item.get("detail") or ""))
        prefix = f"{m.group(1)} × " if m else ""
        lines.append(
            f"{prefix}{item.get('label')} — {format_amount(int(item.get('amount') or 0), currency)}"
        )
    lines.append(f"Total: {format_amount(int(quote.get('total') or 0), currency)}")
    lines.append(TEAM_LINE)
    return "\n".join(lines)
