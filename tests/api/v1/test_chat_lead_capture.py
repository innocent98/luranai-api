"""Chat handler sales orchestration — lead capture integration tests.

Tests cover:
  1. Trigger-intent message creates an anonymous lead (sales mode ON).
  2. Second turn enriches the same lead and sets meta.lead_captured on first email.
  3. SUPPORT intent with no prior lead → no lead created.
  4. Sales mode OFF → plain path, no leads, no lead_captured key.
"""

from unittest.mock import patch
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.lead import Lead
from app.db.models.sales_agent_config import TenantSalesAgentConfig
from app.db.models.tenant import Tenant, TenantAIConfig, TenantCapabilities


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tenant(
    db: Session,
    *,
    sales_enabled: bool,
    meeting_booking_enabled: bool = False,
) -> UUID:
    """Create a Tenant + TenantAIConfig + TenantCapabilities.

    If sales_enabled, also persist a TenantSalesAgentConfig with
    agent_enabled=True, lead_capture_enabled=True and the given
    meeting_booking_enabled flag (default False).

    Returns the tenant UUID so callers don't hold a detached ORM object.
    """
    prefix = uuid4().hex[:8]
    tenant = Tenant(
        id=uuid4(),
        name=f"tenant_{prefix}",
        display_name=f"Tenant {prefix}",
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

    if sales_enabled:
        sales_cfg = TenantSalesAgentConfig(
            id=uuid4(),
            tenant_id=tenant.id,
            agent_enabled=True,
            lead_capture_enabled=True,
            meeting_booking_enabled=meeting_booking_enabled,
            trigger_intents=[
                "PRICING_INQUIRY",
                "DEMO_REQUEST",
                "PURCHASE_INTENT",
                "ENTERPRISE_PROSPECT",
            ],
            qualification_questions=[
                {"key": "company_size", "question": "How large is your team?", "type": "text"}
            ],
        )
        db.add(sales_cfg)

    db.commit()
    return tenant.id


def _count_leads(db: Session, tenant_id: UUID) -> int:
    return len(
        db.execute(select(Lead).where(Lead.tenant_id == tenant_id)).scalars().all()
    )


def _only_lead(db: Session, tenant_id: UUID) -> Lead:
    rows = db.execute(
        select(Lead).where(Lead.tenant_id == tenant_id)
    ).scalars().all()
    assert len(rows) == 1, f"Expected 1 lead, got {len(rows)}"
    return rows[0]


def _sales_llm_patch(intent: str, captured: dict, signals: dict):
    """Return a patcher for RAGService.generate_sales_response."""

    async def _fake(self, prompt, config, *, model_name=None):
        reply = "Sounds great! Let me help you."
        sales_meta = {"intent": intent, "captured": captured, "signals": signals}
        return reply, sales_meta, 50, False

    return patch(
        "app.services.rag_service.RAGService.generate_sales_response",
        new=_fake,
    )


def _plain_llm_patch():
    """Return a patcher for RAGService.generate_ai_response."""

    async def _fake(self, prompt, max_tokens=800, model_name=None, system_message=None):
        return "Here is your answer.", 30, False

    return patch(
        "app.services.rag_service.RAGService.generate_ai_response",
        new=_fake,
    )


def _retrieval_patch():
    """Stub vector retrieval to return empty (no knowledge needed)."""

    async def _fake(self, *, tenant_id, query, max_chunks, score_threshold):
        return [], []

    return patch(
        "app.services.rag_service.RAGService.retrieve_relevant_chunks",
        new=_fake,
    )


def _automation_patch():
    """Stub ChatAutomationOrchestrator so it never handles a turn."""
    from app.services.chat_automation_orchestrator import AutomationTurnResult

    async def _fake(self, **kwargs):
        return AutomationTurnResult(handled=False)

    return patch(
        "app.services.chat_automation_orchestrator.ChatAutomationOrchestrator.handle_turn",
        new=_fake,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_trigger_intent_creates_anonymous_lead(client, db: Session):
    """PRICING_INQUIRY + sales mode ON → anonymous lead created, source=chat_widget."""
    tenant_id = _make_tenant(db, sales_enabled=True)

    with _sales_llm_patch("PRICING_INQUIRY", {}, {}), _retrieval_patch(), _automation_patch():
        r = client.post(
            "/api/v1/chat/message",
            json={
                "message": "how much for bulk?",
                "tenant_id": str(tenant_id),
                "user_identifier": "v1",
            },
        )

    assert r.status_code == 200, r.text

    lead = _only_lead(db, tenant_id)
    assert lead.email is None
    assert lead.source == "chat_widget"
    assert lead.intent == "PRICING_INQUIRY"


def test_enrich_same_lead_and_lead_captured_meta(client, db: Session):
    """Two turns: first creates anon lead; second enriches email → lead_captured in meta."""
    tenant_id = _make_tenant(db, sales_enabled=True)

    # Turn 1 — trigger intent, no captured data
    with _sales_llm_patch("PRICING_INQUIRY", {}, {}), _retrieval_patch(), _automation_patch():
        r1 = client.post(
            "/api/v1/chat/message",
            json={
                "message": "pricing?",
                "tenant_id": str(tenant_id),
                "user_identifier": "v1",
            },
        )
    assert r1.status_code == 200, r1.text
    sid = r1.json()["session_id"]

    # Turn 2 — email captured this turn
    captured = {
        "name": "John",
        "email": "john@co.com",
        "company": None,
        "team_size": "200",
        "industry": None,
    }
    signals = {
        "asked_features": False,
        "requested_demo": True,
        "mentioned_competitor": False,
        "ready_to_buy": False,
    }
    with _sales_llm_patch("ENTERPRISE_PROSPECT", captured, signals), _retrieval_patch(), _automation_patch():
        r2 = client.post(
            "/api/v1/chat/message",
            json={
                "message": "John, john@co.com, 200 staff",
                "session_id": sid,
                "tenant_id": str(tenant_id),
                "user_identifier": "v1",
            },
        )
    assert r2.status_code == 200, r2.text

    meta = r2.json()["message"]["meta"] or {}
    assert meta.get("lead_captured") == {"name": "John", "email": "john@co.com"}

    # Only one lead should exist (same conversation enriched, not duplicated)
    assert _count_leads(db, tenant_id) == 1


def test_support_intent_creates_no_lead(client, db: Session):
    """SUPPORT intent with no prior lead → no lead row created."""
    tenant_id = _make_tenant(db, sales_enabled=True)

    with _sales_llm_patch("SUPPORT", {}, {}), _retrieval_patch(), _automation_patch():
        r = client.post(
            "/api/v1/chat/message",
            json={
                "message": "reset password",
                "tenant_id": str(tenant_id),
                "user_identifier": "v1",
            },
        )
    assert r.status_code == 200, r.text

    assert _count_leads(db, tenant_id) == 0


def test_sales_mode_off_uses_plain_path(client, db: Session):
    """Sales mode OFF → plain AI path used, no leads created, no lead_captured in meta."""
    tenant_id = _make_tenant(db, sales_enabled=False)

    with _plain_llm_patch(), _retrieval_patch(), _automation_patch():
        r = client.post(
            "/api/v1/chat/message",
            json={
                "message": "pricing?",
                "tenant_id": str(tenant_id),
                "user_identifier": "v1",
            },
        )
    assert r.status_code == 200, r.text

    assert _count_leads(db, tenant_id) == 0

    meta = r.json()["message"]["meta"] or {}
    assert "lead_captured" not in meta


def test_lead_captured_not_re_emitted_on_subsequent_turns(client, db: Session):
    """lead_captured is set only when email is FIRST captured, not on later turns."""
    tenant_id = _make_tenant(db, sales_enabled=True)

    # Turn 1 — trigger intent, no captured data
    with _sales_llm_patch("PRICING_INQUIRY", {}, {}), _retrieval_patch(), _automation_patch():
        r1 = client.post(
            "/api/v1/chat/message",
            json={
                "message": "pricing?",
                "tenant_id": str(tenant_id),
                "user_identifier": "v1",
            },
        )
    assert r1.status_code == 200, r1.text
    sid = r1.json()["session_id"]

    # Turn 2 — email captured this turn → lead_captured in meta
    captured = {"name": "John", "email": "john@co.com"}
    signals = {}
    with _sales_llm_patch("ENTERPRISE_PROSPECT", captured, signals), _retrieval_patch(), _automation_patch():
        r2 = client.post(
            "/api/v1/chat/message",
            json={
                "message": "John john@co.com",
                "session_id": sid,
                "tenant_id": str(tenant_id),
                "user_identifier": "v1",
            },
        )
    assert r2.status_code == 200, r2.text
    assert r2.json()["message"]["meta"].get("lead_captured")

    # Turn 3 — same email, no re-emit
    with _sales_llm_patch("ENTERPRISE_PROSPECT", captured, signals), _retrieval_patch(), _automation_patch():
        r3 = client.post(
            "/api/v1/chat/message",
            json={
                "message": "thanks",
                "session_id": sid,
                "tenant_id": str(tenant_id),
                "user_identifier": "v1",
            },
        )
    assert r3.status_code == 200, r3.text
    assert "lead_captured" not in (r3.json()["message"]["meta"] or {})


# ---------------------------------------------------------------------------
# Task 5 (Slice 3): offer_meeting gate tests
# ---------------------------------------------------------------------------

_OFFER_MEETING_SIGNALS = {
    "asked_features": False,
    "requested_demo": True,
    "mentioned_competitor": False,
    "ready_to_buy": False,
    "offer_meeting": True,
}


def test_offer_meeting_gate_off_suppresses_flag(client, db: Session):
    """sales_mode=ON, signals.offer_meeting=True, meeting_booking_enabled=False
    → meta must NOT contain 'offer_meeting'.

    This is the authoritative server-side gate test: the model signals a meeting
    but the tenant has not enabled the meeting-booking feature, so the flag must
    be stripped before it reaches the client.
    """
    tenant_id = _make_tenant(db, sales_enabled=True, meeting_booking_enabled=False)

    with (
        _sales_llm_patch("DEMO_REQUEST", {}, _OFFER_MEETING_SIGNALS),
        _retrieval_patch(),
        _automation_patch(),
    ):
        r = client.post(
            "/api/v1/chat/message",
            json={
                "message": "I'd love to book a demo",
                "tenant_id": str(tenant_id),
                "user_identifier": "v1",
            },
        )

    assert r.status_code == 200, r.text
    meta = r.json()["message"]["meta"] or {}
    assert "offer_meeting" not in meta, (
        f"offer_meeting must be absent when meeting_booking_enabled=False; got meta={meta}"
    )


def test_offer_meeting_gate_on_surfaces_flag(client, db: Session):
    """sales_mode=ON, signals.offer_meeting=True, meeting_booking_enabled=True
    → meta['offer_meeting'] is True.

    Companion positive test: when the tenant has enabled meeting booking, the
    signal propagates to the response meta so the widget can surface the CTA.
    """
    tenant_id = _make_tenant(db, sales_enabled=True, meeting_booking_enabled=True)

    with (
        _sales_llm_patch("DEMO_REQUEST", {}, _OFFER_MEETING_SIGNALS),
        _retrieval_patch(),
        _automation_patch(),
    ):
        r = client.post(
            "/api/v1/chat/message",
            json={
                "message": "I'd love to book a demo",
                "tenant_id": str(tenant_id),
                "user_identifier": "v1",
            },
        )

    assert r.status_code == 200, r.text
    meta = r.json()["message"]["meta"] or {}
    assert meta.get("offer_meeting") is True, (
        f"offer_meeting must be True when meeting_booking_enabled=True; got meta={meta}"
    )
