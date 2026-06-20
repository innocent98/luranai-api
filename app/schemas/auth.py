"""Authentication and registration Pydantic schemas."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field


# Company registration schemas (self-service)
class CompanyRegistrationRequest(BaseModel):
    """Schema for self-service company registration."""

    model_config = ConfigDict(extra="forbid")

    # Company information
    company_name: str = Field(
        ...,
        min_length=2,
        max_length=255,
        description="Company name (becomes tenant name)",
    )
    company_display_name: str | None = Field(
        None,
        min_length=2,
        max_length=255,
        description="Display name (defaults to company_name)",
    )

    # Admin user information
    admin_full_name: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Full name of the admin user",
    )
    admin_email: EmailStr = Field(
        ...,
        description="Admin email address (will receive verification email)",
    )

    # Optional audit claim token (public website audit run before signup)
    claim_token: str | None = Field(
        None,
        min_length=1,
        max_length=128,
        description="Claim token from a public audit run before signup (optional)",
    )


class CompanyRegistrationResponse(BaseModel):
    """Schema for company registration response."""

    message: str = Field(..., description="Success message")
    tenant_id: UUID = Field(..., description="Created tenant ID")
    admin_email: str = Field(..., description="Admin email address")
    verification_required: bool = Field(
        ...,
        description="Whether email verification is required",
    )


# Email verification schemas
class EmailVerificationRequest(BaseModel):
    """Schema for email verification."""

    token: str = Field(..., min_length=1, description="Email verification token")


class EmailVerificationResponse(BaseModel):
    """Schema for email verification response."""

    message: str = Field(..., description="Success message")
    verified: bool = Field(..., description="Whether verification was successful")


# Password reset schemas
class PasswordResetRequest(BaseModel):
    """Schema for requesting password reset."""

    email: EmailStr = Field(..., description="User email address")


class PasswordResetResponse(BaseModel):
    """Schema for password reset request response."""

    message: str = Field(..., description="Success message")
    email_sent: bool = Field(..., description="Whether reset email was sent")


class PasswordResetConfirm(BaseModel):
    """Schema for confirming password reset with token."""

    token: str = Field(..., min_length=1, description="Password reset token")
    new_password: str = Field(
        ...,
        min_length=8,
        max_length=100,
        description="New password",
    )


class PasswordResetConfirmResponse(BaseModel):
    """Schema for password reset confirmation response."""

    message: str = Field(..., description="Success message")
    password_changed: bool = Field(
        ...,
        description="Whether password was successfully changed",
    )


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int = Field(..., description="Token expiration in seconds")
    must_change_password: bool = Field(
        default=False, description="Whether user must change password on next login"
    )


class ForcePasswordChangeRequest(BaseModel):
    new_password: str = Field(..., min_length=8, max_length=100, description="New password to set")
