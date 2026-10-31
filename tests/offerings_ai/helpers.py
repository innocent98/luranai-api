"""Shared builders for the offerings grounding tests (fake data only)."""

from unittest.mock import patch
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from app.db.models.offering import (
    Offering,
    OfferingAvailability,
    OfferingKind,
    OfferingPriceMode,
    OfferingStatus,
    TenantOfferingsSettings,
)
from app.db.models.sales_agent_config import TenantSalesAgentConfig
from app.db.models.tenant import Tenant, TenantAIConfig, TenantCapabilities


def make_tenant(
    db: Session,
    *,
    sales_enabled: bool = False,
    quotation_enabled: bool = False,
    sales_close: bool = True,
    notify_channels: dict | None = None,
    display_name: str = "Fixture Shoes",
) -> UUID:
    """A tenant with a fixed display name so prompts are comparable across runs."""
    tenant = Tenant(
        id=uuid4(),
        name=f"tenant_{uuid4().hex[:8]}",
        display_name=display_name,
        api_key_hash=uuid4().hex,
        subscription_tier="trial",
        subscription_status="trialing",
    )
    db.add(tenant)
    db.flush()
    db.add(
        TenantAIConfig(
            id=uuid4(),
            tenant_id=tenant.id,
            brand_voice={},
            escalation_keywords=[],
            max_response_tokens=250,
            grounding_requirement=True,
        )
    )
    db.add(
        TenantCapabilities(
            id=uuid4(),
            tenant_id=tenant.id,
            can_create_tickets=True,
            can_access_analytics=True,
            can_upload_knowledge=True,
            max_conversations_per_month=1000,
            can_use_sales_close=sales_close,
            can_use_crm_notifications=True,
        )
    )
    if sales_enabled:
        db.add(
            TenantSalesAgentConfig(
                id=uuid4(),
                tenant_id=tenant.id,
                agent_enabled=True,
                lead_capture_enabled=True,
                quotation_enabled=quotation_enabled,
                meeting_booking_enabled=False,
                notify_channels=notify_channels
                or {"email": False, "slack": False, "in_app": True, "crm": False},
                trigger_intents=["PRICING_INQUIRY", "DEMO_REQUEST", "PURCHASE_INTENT"],
                qualification_questions=[
                    {"key": "need", "question": "What are you looking for?", "type": "text"}
                ],
            )
        )
    db.commit()
    return tenant.id


def add_offering(
    db: Session,
    tenant_id: UUID,
    seq: int,
    name: str,
    *,
    price: int | None = 12000,
    price_mode: OfferingPriceMode = OfferingPriceMode.FIXED,
    availability: OfferingAvailability = OfferingAvailability.IN_STOCK,
    kind: OfferingKind = OfferingKind.PRODUCT,
    category: str | None = None,
    keywords: str | None = None,
    description: str | None = None,
    negotiable: bool = False,
    quantity: int | None = None,
    price_note: str | None = None,
    status: OfferingStatus = OfferingStatus.ACTIVE,
) -> Offering:
    if price_mode == OfferingPriceMode.ON_REQUEST:
        price = None
    row = Offering(
        id=uuid4(),
        tenant_id=tenant_id,
        seq=seq,
        kind=kind,
        name=name,
        description=description,
        category=category,
        keywords=keywords,
        price_mode=price_mode,
        price=price,
        price_note=price_note,
        availability=availability,
        quantity=quantity,
        negotiable=negotiable,
        status=status,
    )
    db.add(row)
    db.commit()
    return row


def set_settings(db: Session, tenant_id: UUID, **fields) -> TenantOfferingsSettings:
    row = TenantOfferingsSettings(tenant_id=tenant_id, **fields)
    db.add(row)
    db.commit()
    return row


def retrieval_patch():
    async def _fake(self, *, tenant_id, query, max_chunks, score_threshold):
        return [], []

    return patch("app.services.rag_service.RAGService.retrieve_relevant_chunks", new=_fake)


def automation_patch():
    from app.services.chat_automation_orchestrator import AutomationTurnResult

    async def _fake(self, **kwargs):
        return AutomationTurnResult(handled=False)

    return patch(
        "app.services.chat_automation_orchestrator.ChatAutomationOrchestrator.handle_turn",
        new=_fake,
    )


def send(client, tenant_id: UUID, message: str, *, user: str = "v1", session_id=None):
    body = {"message": message, "tenant_id": str(tenant_id), "user_identifier": user}
    if session_id:
        body["session_id"] = str(session_id)
    return client.post("/api/v1/chat/message", json=body)
