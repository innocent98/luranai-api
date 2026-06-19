"""Pydantic schemas for the Website Auditor endpoints."""

from uuid import UUID

from pydantic import BaseModel, EmailStr, HttpUrl


class PublicAuditRequest(BaseModel):
    url: HttpUrl


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
    pages_scanned: int
    gaps: list[BlurredGap]


class LeadCaptureRequest(BaseModel):
    email: EmailStr
