"""Pydantic v2 schemas for the Leads API."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.db.models.lead import LeadStatus


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


class LeadSignal(BaseModel):
    key: str
    label: str
    weight: int
    fired: bool


class LeadSourceInfo(BaseModel):
    landing_page: str | None = None
    utm: dict | None = None
    channel: str | None = None
    first_seen: datetime | None = None


class LeadCrm(BaseModel):
    """CRM delivery state for one lead.

    ``status`` is the real signal, read from the lead's CRM NotificationLog:
    "sent" | "failed" | "pending", or None when no push was ever attempted —
    which is deliberately distinct from a failure.

    ``connected`` is retained with its original bool(crm_id) meaning so this
    payload stays additive for other /api/v1 clients. Prefer ``status``: crm_id
    is only written when the webhook echoes an id back, and Zapier/Make answer
    2xx with no id, so ``connected`` reads False for leads that did deliver.
    """

    connected: bool = False
    status: str | None = None
    synced_at: datetime | None = None
    error: str | None = None


def _crm_state(lead, crm_log) -> LeadCrm:
    """Map a lead + its CRM NotificationLog to the payload's crm block."""
    if crm_log is None:
        # Never attempted. Not a failure — the UI must distinguish the two.
        return LeadCrm(connected=bool(lead.crm_id))

    status = crm_log.status
    return LeadCrm(
        connected=bool(lead.crm_id),
        status=status.value if hasattr(status, "value") else str(status),
        synced_at=crm_log.updated_at or crm_log.created_at,
        error=crm_log.error,
    )


class LeadDetail(BaseModel):
    """Full lead detail — superset of LeadListItem."""

    # Fields from LeadListItem
    id: uuid.UUID
    name: str | None
    company: str | None
    intent: str | None
    lead_score: int | None
    tier: str
    status: str
    source_label: str
    created_at: datetime

    # Additional detail fields
    email: str | None = None
    team_size: str | None = None
    industry: str | None = None
    conversation_id: uuid.UUID | None = None
    score_breakdown: list[LeadSignal] = Field(default_factory=list)
    source: LeadSourceInfo = Field(default_factory=LeadSourceInfo)
    meeting: dict | None = None
    quote: dict | None = None
    crm: LeadCrm = Field(default_factory=LeadCrm)

    model_config = {"from_attributes": True}

    @classmethod
    def from_lead(
        cls,
        lead,
        tier: str,
        crm_log=None,
        meeting: dict | None = None,
        quote: dict | None = None,
    ) -> LeadDetail:
        """Build the detail payload.

        ``crm_log`` is the lead's CRM NotificationLog row, or None when no push
        was attempted. Passed in rather than queried here so this schema stays
        free of database access.
        """
        breakdown = [
            LeadSignal(
                key=s["key"],
                label=s["label"],
                weight=s["weight"],
                fired=bool(s["fired"]),
            )
            for s in (lead.score_breakdown or [])
        ]

        utm = lead.utm or {}
        paid_hints = ("cpc", "ppc", "paid", "ad", "ads", "adwords", "google_ads")
        utm_vals = " ".join(str(v).lower() for v in utm.values())
        channel = "paid" if any(h in utm_vals for h in paid_hints) else utm.get("utm_medium")

        source_info = LeadSourceInfo(
            landing_page=lead.source,
            utm=utm if utm else None,
            channel=channel,
            first_seen=lead.created_at,
        )

        return cls(
            id=lead.id,
            name=lead.name,
            company=lead.company,
            intent=lead.intent,
            lead_score=lead.lead_score,
            tier=tier,
            status=lead.status.value if hasattr(lead.status, "value") else str(lead.status),
            source_label=_derive_source_label(lead.source, utm),
            created_at=lead.created_at,
            email=lead.email,
            team_size=lead.team_size,
            industry=lead.industry,
            conversation_id=lead.conversation_id,
            score_breakdown=breakdown,
            source=source_info,
            meeting=meeting,
            quote=quote,
            crm=_crm_state(lead, crm_log),
        )


class LeadStatusPatch(BaseModel):
    status: LeadStatus
