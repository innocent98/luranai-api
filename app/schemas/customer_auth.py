"""Customer authentication and profile schemas."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.schemas.user import Token


class CustomerRegisterRequest(BaseModel):
    """Customer signup request."""

    model_config = ConfigDict(extra="forbid")

    full_name: str = Field(..., min_length=1, max_length=255)
    email: EmailStr
    password: str = Field(..., min_length=8, max_length=100)
    confirm_password: str = Field(..., min_length=8, max_length=100)
    accepted_terms: bool = Field(..., description="Customer accepted Luran terms")


class CustomerLoginRequest(BaseModel):
    """Customer login request."""

    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    password: str


class CustomerForgotPasswordRequest(BaseModel):
    """Customer forgot-password request."""

    model_config = ConfigDict(extra="forbid")

    email: EmailStr


class CustomerResetPasswordRequest(BaseModel):
    """Customer reset-password confirmation request."""

    model_config = ConfigDict(extra="forbid")

    token: str = Field(..., min_length=1)
    new_password: str = Field(..., min_length=8, max_length=100)


class CustomerEmailVerificationRequest(BaseModel):
    """Customer email verification request."""

    model_config = ConfigDict(extra="forbid")

    token: str = Field(..., min_length=1)


class CustomerResendVerificationRequest(BaseModel):
    """Customer resend verification request."""

    model_config = ConfigDict(extra="forbid")

    email: EmailStr


class CustomerProfileUpdateRequest(BaseModel):
    """Customer profile update payload."""

    model_config = ConfigDict(extra="forbid")

    full_name: str = Field(..., min_length=1, max_length=255)


class CustomerPasswordUpdateRequest(BaseModel):
    """Authenticated customer password update payload."""

    model_config = ConfigDict(extra="forbid")

    current_password: str = Field(..., min_length=8, max_length=100)
    new_password: str = Field(..., min_length=8, max_length=100)


class CustomerTokenData(BaseModel):
    """Decoded customer token data."""

    customer_user_id: UUID
    email: str


class CustomerUserResponse(BaseModel):
    """Customer profile response."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    full_name: str
    email: EmailStr
    chat_user_key: str
    avatar_url: str | None = None
    is_active: bool
    email_verified_at: datetime | None = None
    last_login_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class CustomerAuthResponse(BaseModel):
    """Auth response with token and profile."""

    token: Token
    customer: CustomerUserResponse


class CustomerActionResponse(BaseModel):
    """Simple success response."""

    message: str
