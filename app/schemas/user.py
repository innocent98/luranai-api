"""User and authentication Pydantic schemas."""

from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field, ConfigDict

from app.db.models.user import UserRole


# Base schemas
class UserBase(BaseModel):
    """Base user schema."""

    email: EmailStr = Field(..., description="User email address")
    full_name: str = Field(..., min_length=1, max_length=255, description="Full name")
    role: UserRole = Field(..., description="User role")


# Create schemas
class UserCreate(UserBase):
    """Schema for creating a new user."""

    password: str = Field(..., min_length=8, max_length=100, description="User password")
    tenant_id: Optional[UUID] = Field(None, description="Tenant ID for tenant-specific users")


class UserRegister(BaseModel):
    """Schema for user registration."""

    email: EmailStr
    password: str = Field(..., min_length=8, max_length=100)
    full_name: str = Field(..., min_length=1, max_length=255)


# Update schemas
class UserUpdate(BaseModel):
    """Schema for updating a user."""

    model_config = ConfigDict(extra="forbid")

    full_name: Optional[str] = Field(None, min_length=1, max_length=255)
    role: Optional[UserRole] = None
    is_active: Optional[bool] = None
    tenant_id: Optional[UUID] = None


class UserPasswordUpdate(BaseModel):
    """Schema for updating user password."""

    current_password: str = Field(..., min_length=8, max_length=100)
    new_password: str = Field(..., min_length=8, max_length=100)


# Auth schemas
class LoginRequest(BaseModel):
    """Schema for login request."""

    email: EmailStr
    password: str


class Token(BaseModel):
    """Schema for JWT token response."""

    access_token: str
    token_type: str = "bearer"
    expires_in: int = Field(..., description="Token expiration in seconds")


class TokenData(BaseModel):
    """Schema for token payload data."""

    user_id: UUID
    email: str
    role: UserRole
    tenant_id: Optional[UUID] = None


# Response schemas
class UserResponse(UserBase):
    """Schema for user response."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: Optional[UUID] = None
    is_active: bool
    is_superuser: bool
    last_login_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class UserListResponse(UserBase):
    """Schema for user list response (minimal fields)."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: Optional[UUID] = None
    is_active: bool
    created_at: datetime


class CurrentUserResponse(UserResponse):
    """Schema for current authenticated user response."""

    pass


# User management schemas (for TENANT_ADMIN)
class UserInviteRequest(BaseModel):
    """Schema for inviting a team member (TENANT_ADMIN creates user)."""

    model_config = ConfigDict(extra="forbid")

    email: EmailStr = Field(..., description="User email address")
    full_name: str = Field(..., min_length=1, max_length=255, description="Full name")
    role: UserRole = Field(
        ...,
        description="User role (must be SUPPORT_AGENT or VIEWER)",
    )


class UserRegeneratePasswordResponse(BaseModel):
    """Schema for password regeneration response."""

    message: str = Field(..., description="Success message")
    temp_password: str = Field(..., description="New temporary password")
    email_sent: bool = Field(..., description="Whether email was sent")
