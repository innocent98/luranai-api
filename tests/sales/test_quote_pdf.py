from uuid import uuid4

from app.db.models.quote import Quote, QuoteStatus
from app.db.models.tenant import Tenant
from app.db.models.lead import Lead
from app.services.quote_pdf_service import render_quote_pdf


def _quote(db):
    t = Tenant(name=f"t_{uuid4().hex[:8]}", display_name="Acme", api_key_hash=uuid4().hex,
               subscription_tier="trial", subscription_status="trialing")
    db.add(t); db.flush()
    lead = Lead(tenant_id=t.id, conversation_id=uuid4(), email="v@co.com", company="Northwind")
    db.add(lead); db.flush()
    q = Quote(tenant_id=t.id, lead_id=lead.id, currency="NGN", total=62050,
              line_items=[{"label": "Growth", "detail": "11–50 users", "amount": 45000},
                          {"label": "Extra seats (×8)", "detail": "₦3,500 / seat", "amount": 28000},
                          {"label": "Annual discount", "detail": "−15%", "amount": -10950}],
              status=QuoteStatus.DRAFT)
    db.add(q); db.flush(); db.refresh(q)
    return q, lead


def test_render_quote_pdf_returns_pdf_bytes(db_session):
    q, lead = _quote(db_session)
    data = render_quote_pdf(q, lead=lead)
    assert isinstance(data, bytes) and len(data) > 0
    assert data[:5] == b"%PDF-"  # PDF magic header


def test_render_single_line_quote(db_session):
    q, lead = _quote(db_session)
    q.line_items = [{"label": "Starter", "detail": "1–10 users", "amount": 20000}]
    q.total = 20000
    data = render_quote_pdf(q, lead=lead)
    assert data[:5] == b"%PDF-"


# ---------------------------------------------------------------------------
# Currency glyphs: the PDF must carry a font that has them (Helvetica has no U+20A6)
# ---------------------------------------------------------------------------

import io
import logging

import PyPDF2
import pytest

from app.services import quote_pdf_service


def _pdf_text(data: bytes) -> str:
    reader = PyPDF2.PdfReader(io.BytesIO(data))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def _fonts(page):
    fonts = page["/Resources"].get_object().get("/Font")
    return list(fonts.get_object().values()) if fonts is not None else []


def _pdf_font_names(data: bytes) -> set[str]:
    reader = PyPDF2.PdfReader(io.BytesIO(data))
    names: set[str] = set()
    for page in reader.pages:
        for font in _fonts(page):
            names.add(str(font.get_object()["/BaseFont"]))
    return names


def _embedded_font_files(data: bytes) -> int:
    reader = PyPDF2.PdfReader(io.BytesIO(data))
    count = 0
    for page in reader.pages:
        for font in _fonts(page):
            font = font.get_object()
            descriptor = font.get("/FontDescriptor")
            if descriptor is not None and "/FontFile2" in descriptor.get_object():
                count += 1
    return count


def test_ngn_quote_pdf_contains_naira_sign_and_embeds_the_font(db_session):
    q, lead = _quote(db_session)
    data = render_quote_pdf(q, lead=lead)
    text = _pdf_text(data)
    assert "₦62,050" in text
    assert "■" not in text
    assert any("DejaVu" in n for n in _pdf_font_names(data))
    assert _embedded_font_files(data) >= 1  # embedded, not looked up on the reader's machine


@pytest.mark.parametrize(
    "currency,symbol", [("USD", "$"), ("GBP", "£"), ("EUR", "€"), ("NGN", "₦")]
)
def test_every_supported_currency_symbol_renders(db_session, currency, symbol):
    q, lead = _quote(db_session)
    q.currency = currency
    q.line_items = [{"label": "Pro", "detail": "−15% · 11–50 users", "amount": 1200}]
    q.total = 1200
    text = _pdf_text(render_quote_pdf(q, lead=lead))
    assert f"{symbol}1,200" in text


def test_yoruba_company_name_survives(db_session):
    q, lead = _quote(db_session)
    lead.company = "Ọmọlará Ṣẹ́ẹ̀"  # Latin Extended Additional + combining marks
    assert "Ọmọlará" in _pdf_text(render_quote_pdf(q, lead=lead))


def test_missing_font_falls_back_without_crashing_and_logs_once(db_session, tmp_path, monkeypatch):
    # Order-independent capture: Alembic's env.py runs logging.config.fileConfig()
    # (disable_existing_loggers=True) in any migration test that ran earlier in the same process,
    # which disables already-imported loggers and leaves caplog empty. So re-enable this exact
    # logger and attach our own handler instead of relying on caplog/propagation.
    log = logging.getLogger("app.services.quote_pdf_service")
    records: list[logging.LogRecord] = []

    class _Collect(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = _Collect(level=logging.WARNING)
    monkeypatch.setattr(log, "disabled", False)
    monkeypatch.setattr(log, "level", logging.WARNING)
    log.addHandler(handler)
    monkeypatch.setattr(quote_pdf_service, "_FONT_DIR", tmp_path)  # no font files here
    monkeypatch.setattr(quote_pdf_service, "_warned_missing_font", False)
    try:
        q, lead = _quote(db_session)
        first = render_quote_pdf(q, lead=lead)
        second = render_quote_pdf(q, lead=lead)
    finally:
        log.removeHandler(handler)
    assert first[:5] == b"%PDF-" and second[:5] == b"%PDF-"
    assert not any("DejaVu" in n for n in _pdf_font_names(first))  # the old Helvetica path
    warnings = [r for r in records if "quote pdf font" in r.getMessage().lower()]
    assert len(warnings) == 1
