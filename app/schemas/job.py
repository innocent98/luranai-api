"""Pydantic schemas for background job tracking."""

from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field

from app.db.models.job import JobStatus, JobType


class JobBase(BaseModel):
    """Base job schema."""
    job_type: JobType
    total_items: Optional[int] = None


class JobCreate(JobBase):
    """Schema for creating a new job."""
    tenant_id: UUID
    input_data: dict
    created_by: Optional[UUID] = None


class JobUpdate(BaseModel):
    """Schema for updating job progress."""
    status: Optional[JobStatus] = None
    processed_items: Optional[int] = None
    failed_items: Optional[int] = None
    result_data: Optional[dict] = None
    error_message: Optional[str] = None


class JobResponse(JobBase):
    """Response schema for job status."""
    id: UUID
    tenant_id: UUID
    status: JobStatus

    # Progress
    total_items: Optional[int] = None
    processed_items: int = 0
    failed_items: int = 0
    progress_percent: int = Field(0, description="Progress percentage (0-100)")

    # Result
    result_data: Optional[dict] = None
    error_message: Optional[str] = None

    # Timing
    created_at: datetime
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    # User tracking
    created_by: Optional[UUID] = None

    class Config:
        from_attributes = True


class JobListResponse(BaseModel):
    """Response for listing jobs."""
    jobs: list[JobResponse]
    total: int
    page: int
    page_size: int


class DocumentUploadJobResponse(BaseModel):
    """
    Response when document upload is queued for background processing.

    This is returned immediately (202 Accepted) when user uploads a document.
    """
    job_id: UUID
    status: JobStatus
    message: str = "Document upload queued for processing"
    check_status_url: str
    estimated_chunks: Optional[int] = None
    estimated_time_seconds: Optional[int] = None

    class Config:
        from_attributes = True
