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
