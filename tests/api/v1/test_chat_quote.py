"""Chat handler sales orchestration — quotation integration tests (Stage 3 Slice 4).

Tests cover:
  1. quotation_enabled=True + offer_quote signal + team_size → meta.quote present,
     Quote row persisted, lead.quote_id set.
  2. quotation_enabled=False → no meta.quote even when signal is true.
  3. pricing_tiers=[] (empty) → compute_quote returns None → no meta.quote.
"""

from unittest.mock import patch
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.lead import Lead
from app.db.models.quote import Quote
from app.db.models.sales_agent_config import TenantSalesAgentConfig
from app.db.models.tenant import Tenant, TenantAIConfig, TenantCapabilities

# ---------------------------------------------------------------------------
# Re-use patch helpers from Slice 2/3 test module
# ---------------------------------------------------------------------------
from tests.api.v1.test_chat_lead_capture import (
    _automation_patch,
    _retrieval_patch,
    _sales_llm_patch,
)

# ---------------------------------------------------------------------------
# Pricing fixture
# ---------------------------------------------------------------------------

_GROWTH_TIER = {
    "name": "Growth",
    "min_users": 11,
    "max_users": 50,
    "base_price": 45000,
    "per_seat_overage_price": 3500,
}

# team_size=58 → overage_seats=8, overage=28000, subtotal=73000, discount(15%)=10950, total=62050
_TEAM_SIZE = "58"
_PLAN_INTEREST = "Growth"
_EXPECTED_TOTAL = 62050

_OFFER_QUOTE_SIGNALS = {
    "asked_features": False,
    "requested_demo": False,
    "mentioned_competitor": False,
    "ready_to_buy": True,
    "offer_quote": True,
}

_CAPTURED_WITH_TEAM_SIZE = {
    "name": "Alice",
    "email": "alice@example.com",
    "company": "Acme",
    "team_size": _TEAM_SIZE,
    "plan_interest": _PLAN_INTEREST,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_quote_tenant(
    db: Session,
    *,
    quotation_enabled: bool,
    pricing_tiers: list | None = None,
    annual_discount_pct: int = 15,
) -> UUID:
    """Create a Tenant + config seeded for quotation tests."""
    prefix = uuid4().hex[:8]
    tenant = Tenant(
        id=uuid4(),
        name=f"qtenant_{prefix}",
        display_name=f"QuoteTenant {prefix}",
        api_key_hash=uuid4().hex,
        subscription_tier="trial",
        subscription_status="trialing",
    )
    db.add(tenant)
    db.flush()

    ai_config = TenantAIConfig(
        id=uuid4(),
        tenant_id=tenant.id,
        brand_voice={},
        escalation_keywords=[],
        max_response_tokens=250,
        grounding_requirement=True,
    )
    db.add(ai_config)

    capabilities = TenantCapabilities(
        id=uuid4(),
        tenant_id=tenant.id,
        can_create_tickets=True,
        can_access_analytics=True,
        can_upload_knowledge=True,
        max_conversations_per_month=1000,
    )
    db.add(capabilities)

    sales_cfg = TenantSalesAgentConfig(
        id=uuid4(),
        tenant_id=tenant.id,
        agent_enabled=True,
        lead_capture_enabled=True,
        meeting_booking_enabled=False,
        quotation_enabled=quotation_enabled,
        trigger_intents=[
            "PRICING_INQUIRY",
            "DEMO_REQUEST",
            "PURCHASE_INTENT",
            "ENTERPRISE_PROSPECT",
        ],
        qualification_questions=[
            {"key": "team_size", "question": "How large is your team?", "type": "text"}
        ],
        pricing_tiers=pricing_tiers if pricing_tiers is not None else [_GROWTH_TIER],
        currency="NGN",
        annual_discount_pct=annual_discount_pct,
    )
    db.add(sales_cfg)

    db.commit()
    return tenant.id


def _only_lead(db: Session, tenant_id: UUID) -> Lead:
    rows = db.execute(
        select(Lead).where(Lead.tenant_id == tenant_id)
    ).scalars().all()
    assert len(rows) == 1, f"Expected 1 lead, got {len(rows)}"
    return rows[0]


def _quotes_for_tenant(db: Session, tenant_id: UUID) -> list[Quote]:
    return db.execute(
        select(Quote).where(Quote.tenant_id == tenant_id)
    ).scalars().all()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_quote_in_meta_when_enabled(client, db: Session):
    """quotation_enabled=True + offer_quote signal + team_size=58
    → meta.quote present with total 62050, Quote row persisted, lead.quote_id set."""
    tenant_id = _make_quote_tenant(db, quotation_enabled=True)

    with (
        _sales_llm_patch("PRICING_INQUIRY", _CAPTURED_WITH_TEAM_SIZE, _OFFER_QUOTE_SIGNALS),
        _retrieval_patch(),
        _automation_patch(),
    ):
        r = client.post(
            "/api/v1/chat/message",
            json={
                "message": "What will Growth cost for 58 users?",
                "tenant_id": str(tenant_id),
                "user_identifier": "qv1",
            },
        )

    assert r.status_code == 200, r.text
    meta = r.json()["message"]["meta"] or {}

    # meta.quote must be present and correct
    assert "quote" in meta, f"Expected 'quote' in meta; got {meta}"
    quote_meta = meta["quote"]
    assert quote_meta["total"] == _EXPECTED_TOTAL, (
        f"Expected total {_EXPECTED_TOTAL}, got {quote_meta['total']}"
    )
    assert quote_meta["currency"] == "NGN"
    assert isinstance(quote_meta["line_items"], list)
    assert len(quote_meta["line_items"]) > 0

    # Quote row must be persisted
    quotes = _quotes_for_tenant(db, tenant_id)
    assert len(quotes) == 1, f"Expected 1 Quote row, got {len(quotes)}"
    assert quotes[0].total == _EXPECTED_TOTAL
    assert quotes[0].currency == "NGN"

    # Lead.quote_id must be set
    lead = _only_lead(db, tenant_id)
    assert lead.quote_id is not None, "lead.quote_id must be set after quote creation"
    assert lead.quote_id == quotes[0].id


def test_no_quote_when_quotation_disabled(client, db: Session):
    """quotation_enabled=False → meta must NOT contain 'quote' even when offer_quote=True."""
    tenant_id = _make_quote_tenant(db, quotation_enabled=False)

    with (
        _sales_llm_patch("PRICING_INQUIRY", _CAPTURED_WITH_TEAM_SIZE, _OFFER_QUOTE_SIGNALS),
        _retrieval_patch(),
        _automation_patch(),
    ):
        r = client.post(
            "/api/v1/chat/message",
            json={
                "message": "What will Growth cost for 58 users?",
                "tenant_id": str(tenant_id),
                "user_identifier": "qv2",
            },
        )

    assert r.status_code == 200, r.text
    meta = r.json()["message"]["meta"] or {}
    assert "quote" not in meta, (
        f"'quote' must be absent when quotation_enabled=False; got meta={meta}"
    )

    # No Quote row should be persisted
    quotes = _quotes_for_tenant(db, tenant_id)
    assert len(quotes) == 0, f"Expected 0 Quote rows, got {len(quotes)}"


def test_no_quote_when_pricing_unconfigured(client, db: Session):
    """pricing_tiers=[] → compute_quote returns None → no meta.quote."""
    tenant_id = _make_quote_tenant(db, quotation_enabled=True, pricing_tiers=[])

    with (
        _sales_llm_patch("PRICING_INQUIRY", _CAPTURED_WITH_TEAM_SIZE, _OFFER_QUOTE_SIGNALS),
        _retrieval_patch(),
        _automation_patch(),
    ):
        r = client.post(
            "/api/v1/chat/message",
            json={
                "message": "What will Growth cost for 58 users?",
                "tenant_id": str(tenant_id),
                "user_identifier": "qv3",
            },
        )

    assert r.status_code == 200, r.text
    meta = r.json()["message"]["meta"] or {}
    assert "quote" not in meta, (
        f"'quote' must be absent when pricing_tiers=[]; got meta={meta}"
    )

    # No Quote row should be persisted
    quotes = _quotes_for_tenant(db, tenant_id)
    assert len(quotes) == 0, f"Expected 0 Quote rows, got {len(quotes)}"
