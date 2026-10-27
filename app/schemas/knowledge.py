"""Knowledge management Pydantic schemas."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, computed_field

from app.db.models.knowledge import ChunkVisibility, SourceStatus, SourceType
from app.utils.knowledge_failures import failure_message


class FailureReasonOut(BaseModel):
    """A machine-readable code plus a message that is safe to show the tenant."""

    code: str
    message: str


# Base schemas
class KnowledgeSourceBase(BaseModel):
    """Base knowledge source schema."""

    name: str = Field(..., min_length=1, max_length=500, description="Source name or filename")
    source_type: SourceType = Field(..., description="Type of knowledge source")
    meta: dict | None = Field(None, description="Additional metadata")


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
    file_url: str | None = Field(None, max_length=1000, description="Storage location URL")
    content_hash: str = Field(..., min_length=32, max_length=64, description="Content hash")
    uploaded_by: UUID | None = Field(None, description="User who uploaded this source")
    source_category: str = Field(default="file", description="Source category: file, url")
    source_url: str | None = Field(None, max_length=2048, description="Original URL")
    file_size_bytes: int | None = Field(None, ge=0, description="File size in bytes")


class DocumentUploadRequest(BaseModel):
    """Schema for document upload request."""

    tenant_id: UUID = Field(..., description="Tenant ID")
    document_name: str = Field(..., min_length=1, max_length=500, description="Document filename")
    # Length is validated by the endpoint (422 `document_too_short`), not here, so the error
    # body has the same flat shape as every other upload failure.
    document_text: str = Field(..., description="Full document text content")
    source_type: SourceType = Field(default=SourceType.HELP_DOC, description="Type of document")
    metadata: dict | None = Field(None, description="Additional document metadata")


class KnowledgeChunkCreate(KnowledgeChunkBase):
    """Schema for creating a knowledge chunk."""

    tenant_id: UUID
    source_id: UUID
    chunk_index: int = Field(..., ge=0, description="Position in original document")
    vector_id: str | None = Field(
        None, min_length=1, max_length=100, description="Qdrant vector ID (set after embedding)"
    )
    version: int = Field(default=1, ge=1, description="Version number")


class KnowledgeUploadRequest(BaseModel):
    """Schema for uploading knowledge documents."""

    name: str = Field(..., min_length=1, max_length=500)
    source_type: SourceType
    content: str = Field(..., min_length=1, description="Document content to be chunked")
    meta: dict | None = Field(None, description="Additional metadata")


# Update schemas
class KnowledgeSourceUpdate(BaseModel):
    """Schema for updating a knowledge source."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(None, min_length=1, max_length=500)
    status: SourceStatus | None = None
    meta: dict | None = None


# Response schemas
class KnowledgeChunkResponse(KnowledgeChunkBase):
    """Schema for knowledge chunk response."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    source_id: UUID
    chunk_index: int
    vector_id: str | None  # Can be None before embedding generation
    version: int
    is_active: bool
    created_at: datetime
    updated_at: datetime


class KnowledgeSourceResponse(KnowledgeSourceBase):
    """Schema for knowledge source response."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    file_url: str | None
    content_hash: str
    version: int
    status: SourceStatus
    chunk_count: int
    uploaded_by: UUID | None
    source_category: str
    source_url: str | None
    file_size_bytes: int | None
    created_at: datetime
    updated_at: datetime
    failure_code: str | None = Field(default=None, exclude=True)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def failure_reason(self) -> "FailureReasonOut | None":
        """Why a FAILED source failed: `{code, message}`; null for every other source."""
        if not self.failure_code:
            return None
        return FailureReasonOut(
            code=self.failure_code, message=failure_message(self.failure_code) or ""
        )


class KnowledgeSourceDetailResponse(KnowledgeSourceResponse):
    """Schema for knowledge source with chunks."""

    chunks: list[KnowledgeChunkResponse] = Field(default_factory=list)


class KnowledgeUploadResponse(BaseModel):
    """Schema for knowledge upload response."""

    source_id: UUID
    status: SourceStatus
    chunk_count: int
    message: str
