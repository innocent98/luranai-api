"""Render a Quote to a PDF (Stage 3 Slice 5b). Pure-pip xhtml2pdf; no system deps."""

import logging
from io import BytesIO
from pathlib import Path

from xhtml2pdf import pisa

from app.sales.pricing import _currency_symbol
from app.services.email_service import EmailService


logger = logging.getLogger(__name__)

# The PDF's built-in Helvetica has no naira sign (U+20A6) and prints a black square for it, so the
# quote embeds DejaVu Sans (subset; licence next to the files). The files ship inside the image
# with the rest of `app/`; nothing is fetched at runtime.
_FONT_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"
_warned_missing_font = False


def _font_context() -> dict:
    """Template variables that switch the PDF to the embedded font; empty when it is missing,
    which keeps the old Helvetica rendering (logged once, never a crash)."""
    global _warned_missing_font
    regular, bold = _FONT_DIR / "DejaVuSans.ttf", _FONT_DIR / "DejaVuSans-Bold.ttf"
    if regular.is_file() and bold.is_file():
        return {"font_regular": regular.as_posix(), "font_bold": bold.as_posix()}
    if not _warned_missing_font:
        _warned_missing_font = True
        logger.warning(
            "quote PDF font files missing in %s: using Helvetica, so the naira sign prints as a box",
            _FONT_DIR,
        )
    return {}


def _fmt(amount: int, currency: str) -> str:
    sym = _currency_symbol(currency)
    sign = "−" if amount < 0 else ""
    return f"{sign}{sym}{abs(int(amount)):,}"


def render_quote_pdf(quote, *, lead=None) -> bytes:
    """Render `quote` to PDF bytes. Raises RuntimeError if generation fails."""
    items = [
        {
            "label": li.get("label", ""),
            "detail": li.get("detail", ""),
            "amount_fmt": _fmt(int(li.get("amount", 0)), quote.currency),
            "negative": int(li.get("amount", 0)) < 0,
        }
        for li in (quote.line_items or [])
    ]
    ctx = {
        "company": (lead.company if lead is not None and lead.company else "Your company"),
        "currency": quote.currency,
        "items": items,
        "total_fmt": _fmt(int(quote.total or 0), quote.currency),
        **_font_context(),
    }
    html = EmailService().render_template("quote_pdf.html", **ctx)
    buf = BytesIO()
    result = pisa.CreatePDF(src=html, dest=buf)
    if result.err:
        raise RuntimeError("quote PDF generation failed")
    return buf.getvalue()
