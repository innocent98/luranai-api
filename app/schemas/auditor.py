"""Pydantic schemas for the Website Auditor endpoints."""

from uuid import UUID

from pydantic import BaseModel, EmailStr, HttpUrl


class PublicAuditRequest(BaseModel):
    url: HttpUrl
    confirmed_target: str | None = None


class BlurredGap(BaseModel):
    title: str
    kind: str
    demand_count: int


class AuditScoresResponse(BaseModel):
    knowledge_score: int | None
    support_score: int | None
    conversion_score: int | None
    overall_score: int | None
    projected_score: int | None


class PublicReportResponse(AuditScoresResponse):
    id: UUID
    status: str
    url: str
    pages_scanned: int
    entered_url: str | None = None
    gaps: list[BlurredGap]
    # Found-content teaser: {"kinds": [...], "entries": N}. Powers the
    # "What we found" section — without it the FE has no found-data and
    # wrongly falls back to showing the gaps as if they were found.
    summary_stats: dict | None = None
    scan_quality: str | None = None
    scan_quality_reason: str | None = None


class LeadCaptureRequest(BaseModel):
    email: EmailStr


class DashboardAuditRequest(BaseModel):
    url: HttpUrl
    confirmed_target: str | None = None


class EntryEditRequest(BaseModel):
    title: str | None = None
    kind: str | None = None
    content: str | None = None
    category: str | None = None
    status: str | None = None


class EntryDetailResponse(BaseModel):
    id: str
    url: str
    title: str | None = None
    category: str | None = None
    kind: str
    content: str
    status: str
    importance_rank: int
    edited: bool
