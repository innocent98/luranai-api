"""What an owner alert says about wanted items: item, quantity, listed price, the customer's offer.

Shared by the in-app/Slack text, the rep e-mail and the CRM payload so the three never disagree.
Amounts carry the tenant's currency symbol. Nothing here is escaped: each channel escapes at render
time (Slack mrkdwn in `workers/sales_notifications.py`, Jinja autoescape in the e-mail).
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.offering import LeadOfferingInterest, TenantOfferingsSettings
from app.db.models.sales_agent_config import TenantSalesAgentConfig
from app.offerings.facts import format_amount


def tenant_currency(db: Session, tenant_id: UUID) -> str:
    row = db.execute(
        select(TenantOfferingsSettings.currency).where(
            TenantOfferingsSettings.tenant_id == tenant_id
        )
    ).scalar_one_or_none()
    if row:
        return row
    return (
        db.execute(
            select(TenantSalesAgentConfig.currency).where(
                TenantSalesAgentConfig.tenant_id == tenant_id
            )
        ).scalar_one_or_none()
        or "NGN"
    )


def interest_lines(db: Session, tenant_id: UUID, lead_id: UUID) -> list[str]:
    currency = tenant_currency(db, tenant_id)
    rows = db.execute(
        select(LeadOfferingInterest)
        .where(LeadOfferingInterest.tenant_id == tenant_id, LeadOfferingInterest.lead_id == lead_id)
        .order_by(LeadOfferingInterest.created_at, LeadOfferingInterest.ref)
    ).scalars()
    lines = []
    for r in rows:
        line = f"{r.qty} × {r.name_snapshot}"
        if r.price_snapshot is not None:
            line += f" (listed {format_amount(r.price_snapshot, currency)})"
        if r.offered_price is not None:
            line += f", customer offered {format_amount(r.offered_price, currency)}"
        lines.append(line)
    return lines
