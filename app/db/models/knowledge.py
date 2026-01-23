"""Knowledge management models for document storage and retrieval."""

import enum
import uuid
from typing import Optional

from sqlalchemy import Boolean, Integer, JSON, String, Text, Enum as SQLEnum, Index, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy import ForeignKey

from app.db.base import Base, UUIDMixin, TimestampMixin


class SourceType(str, enum.Enum):
    """Knowledge source type enum."""

    FAQ = "faq"
    HELP_DOC = "help_doc"
    POLICY = "policy"
    SOP = "sop"
    OTHER = "other"


class SourceStatus(str, enum.Enum):
    """Knowledge source processing status."""

    PROCESSING = "processing"
    ACTIVE = "active"
    INACTIVE = "inactive"
    FAILED = "failed"


class ChunkVisibility(str, enum.Enum):
    """Knowledge chunk visibility level."""

    PUBLIC = "public"
    INTERNAL = "internal"
    RESTRICTED = "restricted"


class KnowledgeSource(Base, UUIDMixin, TimestampMixin):
    """
    Tracks uploaded documents and knowledge sources.

    Represents the original document or data source before chunking.
    Supports versioning to prevent silent overwrites.
    """

    __tablename__ = "knowledge_sources"
    __table_args__ = (
        Index("ix_knowledge_sources_tenant_status", "tenant_id", "status"),
        # Prevent race conditions - only one version per (tenant, content_hash, version) tuple
        UniqueConstraint(
            "tenant_id",
            "content_hash",
            "version",
            name="uq_knowledge_source_tenant_hash_version"
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
        comment="Tenant that owns this knowledge source",
    )
    name: Mapped[str] = mapped_column(
        String(500),
        nullable=False,
        comment="Original filename or source name",
    )
    source_type: Mapped[SourceType] = mapped_column(
        SQLEnum(SourceType, name="source_type"),
        nullable=False,
        index=True,
        comment="Type of knowledge source",
    )
    file_url: Mapped[Optional[str]] = mapped_column(
        String(1000),
        nullable=True,
        comment="Storage location (S3, GCS, etc.) if applicable",
    )
    content_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        index=True,
        comment="SHA256 hash for detecting duplicates",
    )
    version: Mapped[int] = mapped_column(
        Integer,
        default=1,
        nullable=False,
        comment="Version number for this source",
    )
    status: Mapped[SourceStatus] = mapped_column(
        SQLEnum(SourceStatus, name="source_status"),
        default=SourceStatus.PROCESSING,
        nullable=False,
        index=True,
        comment="Processing status of the source",
    )
    chunk_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
        comment="Number of chunks created from this source",
    )
    meta: Mapped[Optional[dict]] = mapped_column(
        JSON,
        nullable=True,
        comment="Additional metadata: {language, author, tags, etc.}",
    )
    uploaded_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        nullable=True,
        comment="User ID who uploaded this source",
    )

    # Relationships
    chunks: Mapped[list["KnowledgeChunk"]] = relationship(
        "KnowledgeChunk",
        back_populates="source",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<KnowledgeSource(id={self.id}, name={self.name}, version={self.version}, status={self.status})>"


class KnowledgeChunk(Base, UUIDMixin, TimestampMixin):
    """
    Individual embedded chunks with metadata for retrieval.

    Represents a processed chunk of knowledge that has been embedded
    and stored in the vector database (Qdrant).
    """

    __tablename__ = "knowledge_chunks"
    __table_args__ = (
        Index("ix_knowledge_chunks_tenant_active", "tenant_id", "is_active"),
        Index("ix_knowledge_chunks_source_active", "source_id", "is_active"),
        Index("ix_knowledge_chunks_vector_id", "vector_id"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
        comment="Tenant that owns this chunk (for isolation)",
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_sources.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
        comment="Parent knowledge source",
    )
    content: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="The actual chunk text (300-500 tokens)",
    )
    chunk_index: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Position in original document",
    )
    vector_id: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        unique=True,
        comment="Reference to Qdrant vector ID",
    )
    meta: Mapped[dict] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
        comment="Additional context: {section, subsection, page_number, etc.}",
    )
    visibility: Mapped[ChunkVisibility] = mapped_column(
        SQLEnum(ChunkVisibility, name="chunk_visibility"),
        default=ChunkVisibility.PUBLIC,
        nullable=False,
        comment="Access control level for this chunk",
    )
    language: Mapped[str] = mapped_column(
        String(10),
        default="en",
        nullable=False,
        comment="Language code (ISO 639-1)",
    )
    version: Mapped[int] = mapped_column(
        Integer,
        default=1,
        nullable=False,
        comment="Version number (matches source version)",
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
        index=True,
        comment="False when replaced by newer version",
    )

    # Relationships
    source: Mapped["KnowledgeSource"] = relationship(
        "KnowledgeSource",
        back_populates="chunks",
    )

    def __repr__(self) -> str:
        return f"<KnowledgeChunk(id={self.id}, source_id={self.source_id}, chunk_index={self.chunk_index}, is_active={self.is_active})>"
