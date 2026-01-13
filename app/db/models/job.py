"""Background job models for async processing."""

from datetime import datetime
from enum import Enum as PyEnum
from uuid import UUID, uuid4

from sqlalchemy import Column, String, Integer, DateTime, Text, JSON, ForeignKey, Enum
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import relationship

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

    id = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id = Column(PGUUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True)

    # Job metadata
    job_type = Column(Enum(JobType), nullable=False, index=True)
    status = Column(Enum(JobStatus), nullable=False, default=JobStatus.QUEUED, index=True)

    # Progress tracking
    total_items = Column(Integer, nullable=True)  # Total chunks, documents, etc.
    processed_items = Column(Integer, default=0)  # Items completed so far
    failed_items = Column(Integer, default=0)  # Items that failed

    # Job details
    input_data = Column(JSON, nullable=False)  # Input parameters (document_name, file_path, etc.)
    result_data = Column(JSON, nullable=True)  # Output data (source_id, chunk_ids, etc.)
    error_message = Column(Text, nullable=True)  # Error details if failed

    # Timing
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    started_at = Column(DateTime, nullable=True)  # When worker picked it up
    completed_at = Column(DateTime, nullable=True)  # When finished (success or failure)

    # User tracking
    created_by = Column(PGUUID(as_uuid=True), ForeignKey("users.id"), nullable=True)

    # Relationships
    tenant = relationship("Tenant", back_populates="processing_jobs")
    created_by_user = relationship("User", foreign_keys=[created_by])

    def __repr__(self):
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
