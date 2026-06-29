"""Render a Quote to a PDF (Stage 3 Slice 5b). Pure-pip xhtml2pdf; no system deps."""

from io import BytesIO

from xhtml2pdf import pisa

from app.sales.pricing import _currency_symbol
from app.services.email_service import EmailService


def _fmt(amount: int, currency: str) -> str:
    sym = _currency_symbol(currency)
    sign = "−" if amount < 0 else ""
    return f"{sign}{sym}{abs(int(amount)):,}"


def render_quote_pdf(quote, *, lead=None) -> bytes:
    """Render `quote` to PDF bytes. Raises RuntimeError if generation fails."""
    items = [
        {"label": li.get("label", ""), "detail": li.get("detail", ""),
         "amount_fmt": _fmt(int(li.get("amount", 0)), quote.currency),
         "negative": int(li.get("amount", 0)) < 0}
        for li in (quote.line_items or [])
    ]
    ctx = {
        "company": (lead.company if lead is not None and lead.company else "Your company"),
        "currency": quote.currency,
        "items": items,
        "total_fmt": _fmt(int(quote.total or 0), quote.currency),
    }
    html = EmailService().render_template("quote_pdf.html", **ctx)
    buf = BytesIO()
    result = pisa.CreatePDF(src=html, dest=buf)
    if result.err:
        raise RuntimeError("quote PDF generation failed")
    return buf.getvalue()
