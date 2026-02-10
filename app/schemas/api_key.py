"""API Key Pydantic schemas."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.db.models.api_key import APIKeyType


# Base schemas
class APIKeyBase(BaseModel):
    """Base API key schema."""

    name: str = Field(
        ..., min_length=1, max_length=100, description="Descriptive name for the API key"
    )
    key_type: APIKeyType = Field(..., description="Type of API key (PUBLIC or SECRET)")


# Create schemas
class APIKeyCreate(APIKeyBase):
    """Schema for creating a new API key."""

    expires_at: datetime | None = Field(None, description="Expiration date (null = never expires)")


# Response schemas
class APIKeyResponse(APIKeyBase):
    """Schema for API key response (without full key)."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    key_prefix: str = Field(..., description="Key prefix for display (e.g., 'pk_live_abc...')")
    is_active: bool
    last_used_at: datetime | None = None
    expires_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class APIKeyCreateResponse(APIKeyResponse):
    """Schema for API key creation response (includes full key once)."""

    full_key: str = Field(..., description="Full API key (only shown once during creation)")


class APIKeyListResponse(BaseModel):
    """Schema for API key list item."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    key_type: APIKeyType
    key_prefix: str
    is_active: bool
    last_used_at: datetime | None = None
    expires_at: datetime | None = None
    created_at: datetime


# Update schemas
class APIKeyUpdate(BaseModel):
    """Schema for updating an API key."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(None, min_length=1, max_length=100)
    is_active: bool | None = None
