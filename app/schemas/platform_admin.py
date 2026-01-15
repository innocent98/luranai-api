"""Platform admin Pydantic schemas."""

from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field, ConfigDict

from app.db.models.platform_admin import PlatformAdminRole


# Base schemas
class PlatformAdminBase(BaseModel):
    """Base platform admin schema."""

    email: EmailStr = Field(..., description="Admin email address")
    full_name: str = Field(..., min_length=1, max_length=255, description="Full name")
    role: PlatformAdminRole = Field(..., description="Admin role")


# Create schemas
class PlatformAdminCreate(PlatformAdminBase):
    """Schema for creating a new platform admin (SUPER_ADMIN only)."""

    password: str = Field(..., min_length=8, max_length=100, description="Admin password")


# Update schemas
class PlatformAdminUpdate(BaseModel):
    """Schema for updating a platform admin."""

    model_config = ConfigDict(extra="forbid")

    full_name: Optional[str] = Field(None, min_length=1, max_length=255)
    role: Optional[PlatformAdminRole] = None
    is_active: Optional[bool] = None


class PlatformAdminPasswordUpdate(BaseModel):
    """Schema for updating admin password."""

    current_password: str = Field(..., min_length=8, max_length=100)
    new_password: str = Field(..., min_length=8, max_length=100)


# Auth schemas
class PlatformAdminLoginRequest(BaseModel):
    """Schema for platform admin login request."""

    email: EmailStr
    password: str


class PlatformAdminToken(BaseModel):
    """Schema for platform admin JWT token response."""

    access_token: str
    token_type: str = "bearer"
    expires_in: int = Field(..., description="Token expiration in seconds")


class PlatformAdminTokenData(BaseModel):
    """Schema for platform admin token payload data."""

    admin_id: UUID
    email: str
    role: PlatformAdminRole


# Response schemas
class PlatformAdminResponse(PlatformAdminBase):
    """Schema for platform admin response."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    is_active: bool
    last_login_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class PlatformAdminListResponse(BaseModel):
    """Schema for platform admin list response (minimal fields)."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: EmailStr
    full_name: str
    role: PlatformAdminRole
    is_active: bool
    created_at: datetime


class CurrentPlatformAdminResponse(PlatformAdminResponse):
    """Schema for current authenticated platform admin response."""

    pass
