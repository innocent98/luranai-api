"""Pydantic schemas for the account-deletion request/verify/cancel/tenant endpoints."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class AccountDeletionRequestBody(BaseModel):
    """Body for the public (unauthenticated) deletion-request path.

    Ignored when the caller is authenticated via bearer token — the authed
    self-serve path resolves identity from the session, not this body.
    """

    model_config = ConfigDict(extra="forbid")

    email: EmailStr | None = Field(
        None, description="Required for the public (unauthenticated) request path"
    )


class AccountDeletionGraceResponse(BaseModel):
    """Response for a successful self-serve/tenant deletion request or verify."""

    message: str
    status: str = "grace"
    scheduled_purge_at: datetime


class AccountDeletionPublicResponse(BaseModel):
    """Fixed, constant-shape response for the public request path.

    Byte-identical regardless of whether the email matches an account —
    every response from that path must be built with the default here, never
    a caller-supplied message.
    """

    message: str = "If an account exists, a verification email has been sent."


class AccountDeletionVerifyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(..., min_length=1)


class AccountDeletionCancelBody(BaseModel):
    """Body for the cancel endpoint — omit `token` when calling authed."""

    model_config = ConfigDict(extra="forbid")

    token: str | None = None


class AccountDeletionCancelResponse(BaseModel):
    message: str = "Account deletion cancelled. Your account is active again."


class TenantDeletionRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirm: bool = Field(
        ..., description="Must be explicitly true to confirm whole-tenant deletion"
    )
