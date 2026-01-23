"""Knowledge management Pydantic schemas."""

from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field, ConfigDict

from app.db.models.knowledge import SourceType, SourceStatus, ChunkVisibility


# Base schemas
class KnowledgeSourceBase(BaseModel):
    """Base knowledge source schema."""

    name: str = Field(..., min_length=1, max_length=500, description="Source name or filename")
    source_type: SourceType = Field(..., description="Type of knowledge source")
    meta: Optional[dict] = Field(None, description="Additional metadata")


class KnowledgeChunkBase(BaseModel):
    """Base knowledge chunk schema."""

    content: str = Field(..., min_length=1, description="Chunk content text")
    meta: dict = Field(default_factory=dict, description="Additional context metadata")
    visibility: ChunkVisibility = Field(
        default=ChunkVisibility.PUBLIC, description="Access control level"
    )
    language: str = Field(default="en", min_length=2, max_length=10, description="Language code")


# Create schemas
class KnowledgeSourceCreate(KnowledgeSourceBase):
    """Schema for creating a knowledge source."""

    tenant_id: UUID = Field(..., description="Tenant that owns this source")
    file_url: Optional[str] = Field(None, max_length=1000, description="Storage location URL")
    content_hash: str = Field(..., min_length=32, max_length=64, description="Content hash")
    uploaded_by: Optional[UUID] = Field(None, description="User who uploaded this source")


class DocumentUploadRequest(BaseModel):
    """Schema for document upload request."""

    tenant_id: UUID = Field(..., description="Tenant ID")
    document_name: str = Field(..., min_length=1, max_length=500, description="Document filename")
    document_text: str = Field(..., min_length=10, description="Full document text content")
    source_type: SourceType = Field(default=SourceType.HELP_DOC, description="Type of document")
    metadata: Optional[dict] = Field(None, description="Additional document metadata")


class KnowledgeChunkCreate(KnowledgeChunkBase):
    """Schema for creating a knowledge chunk."""

    tenant_id: UUID
    source_id: UUID
    chunk_index: int = Field(..., ge=0, description="Position in original document")
    vector_id: Optional[str] = Field(None, min_length=1, max_length=100, description="Qdrant vector ID (set after embedding)")
    version: int = Field(default=1, ge=1, description="Version number")


class KnowledgeUploadRequest(BaseModel):
    """Schema for uploading knowledge documents."""

    name: str = Field(..., min_length=1, max_length=500)
    source_type: SourceType
    content: str = Field(..., min_length=1, description="Document content to be chunked")
    meta: Optional[dict] = Field(None, description="Additional metadata")


# Update schemas
class KnowledgeSourceUpdate(BaseModel):
    """Schema for updating a knowledge source."""

    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(None, min_length=1, max_length=500)
    status: Optional[SourceStatus] = None
    meta: Optional[dict] = None


# Response schemas
class KnowledgeChunkResponse(KnowledgeChunkBase):
    """Schema for knowledge chunk response."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    source_id: UUID
    chunk_index: int
    vector_id: Optional[str]  # Can be None before embedding generation
    version: int
    is_active: bool
    created_at: datetime
    updated_at: datetime


class KnowledgeSourceResponse(KnowledgeSourceBase):
    """Schema for knowledge source response."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    file_url: Optional[str]
    content_hash: str
    version: int
    status: SourceStatus
    chunk_count: int
    uploaded_by: Optional[UUID]
    created_at: datetime
    updated_at: datetime


class KnowledgeSourceDetailResponse(KnowledgeSourceResponse):
    """Schema for knowledge source with chunks."""

    chunks: list[KnowledgeChunkResponse] = Field(default_factory=list)


class KnowledgeUploadResponse(BaseModel):
    """Schema for knowledge upload response."""

    source_id: UUID
    status: SourceStatus
    chunk_count: int
    message: str
