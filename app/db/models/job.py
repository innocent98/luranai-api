"""Background job models for async processing."""

from datetime import datetime
from enum import Enum as PyEnum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import JSON, DateTime, Enum, ForeignKey, Integer, Text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class JobStatus(str, PyEnum):
    """Job processing status."""

    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobType(str, PyEnum):
    """Type of background job."""

    DOCUMENT_INGESTION = "document_ingestion"
    BULK_EMBEDDING = "bulk_embedding"
    KNOWLEDGE_REINDEX = "knowledge_reindex"


class ProcessingJob(Base):
    """
    Background processing job tracker.

    Tracks async operations like document ingestion, bulk processing, etc.
    Used with Redis queue (ARQ) for background task execution.
    """

    __tablename__ = "processing_jobs"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )

    # Job metadata
    job_type: Mapped[JobType] = mapped_column(Enum(JobType), nullable=False, index=True)
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus), nullable=False, default=JobStatus.QUEUED, index=True
    )

    # Progress tracking
    total_items: Mapped[int | None] = mapped_column(Integer, nullable=True)
    processed_items: Mapped[int] = mapped_column(Integer, default=0)
    failed_items: Mapped[int] = mapped_column(Integer, default=0)

    # Job details
    input_data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    result_data: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Timing
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )  # When worker picked it up
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )  # When finished (success or failure)

    # User tracking
    created_by: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )

    # Relationships
    tenant = relationship("Tenant", back_populates="processing_jobs")
    created_by_user = relationship("User", foreign_keys=[created_by])

    def __repr__(self) -> str:
        return f"<ProcessingJob {self.id} {self.job_type.value} {self.status.value}>"

    @property
    def progress_percent(self) -> int:
        """Calculate progress percentage."""
        if not self.total_items or self.total_items == 0:
            return 0
        return int((self.processed_items / self.total_items) * 100)

    @property
    def is_terminal(self) -> bool:
        """Check if job is in terminal state (won't change)."""
        return self.status in [JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED]
