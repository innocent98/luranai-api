"""Pydantic v2 schemas for the Leads API."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


def _derive_source_label(source: str | None, utm: dict | None) -> str:
    """Build a human-readable source label.

    Examples:
        source="website", utm={"utm_medium": "cpc"} -> "website · Paid ad"
        source="/pricing", utm={}                    -> "/pricing"
        source=None, utm=None                        -> "Direct"
    """
    utm = utm or {}
    paid_hints = ("cpc", "ppc", "paid", "ad", "ads", "adwords", "google_ads")
    utm_vals = " ".join(str(v).lower() for v in utm.values())
    is_paid = any(h in utm_vals for h in paid_hints)

    label = source or "Direct"
    if is_paid:
        label = f"{label} · Paid ad"
    elif utm.get("utm_source"):
        label = f"{label} · {utm['utm_source']}"
    return label


class LeadListItem(BaseModel):
    id: uuid.UUID
    name: str | None
    company: str | None
    intent: str | None
    lead_score: int | None
    tier: str
    status: str
    source_label: str
    created_at: datetime

    model_config = {"from_attributes": True}

    @classmethod
    def from_lead(cls, lead, tier: str) -> LeadListItem:
        return cls(
            id=lead.id,
            name=lead.name,
            company=lead.company,
            intent=lead.intent,
            lead_score=lead.lead_score,
            tier=tier,
            status=lead.status.value if hasattr(lead.status, "value") else str(lead.status),
            source_label=_derive_source_label(lead.source, lead.utm),
            created_at=lead.created_at,
        )


class LeadListResponse(BaseModel):
    items: list[LeadListItem]
    total: int
    limit: int
    offset: int


class LeadsSummary(BaseModel):
    new_this_week: int
    avg_score: float
    sales_ready_count: int
    pipeline_value: int = Field(default=0)
    status_counts: dict[str, int]
