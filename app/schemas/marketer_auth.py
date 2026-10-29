"""Marketer authentication request schemas."""

from pydantic import BaseModel, EmailStr, Field


class MarketerLoginRequest(BaseModel):
    email: EmailStr = Field(..., max_length=320)
    # No length floor on login: never reveal the password policy to someone guessing.
    password: str = Field(..., min_length=1, max_length=200)


class MarketerForgotPasswordRequest(BaseModel):
    email: EmailStr = Field(..., max_length=320)


class MarketerSetPasswordRequest(BaseModel):
    token: str = Field(..., min_length=1, max_length=512)
    # Same rules as the tenant and platform-admin password schemas (8-100 characters).
    new_password: str = Field(..., min_length=8, max_length=100)
