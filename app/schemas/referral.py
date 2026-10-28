"""Referral program schemas (platform-admin marketer management)."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.db.models.referral import Marketer, MarketerStatus
from app.services.referral.codes import validate_marketer_code

_ACCOUNT_NUMBER_PATTERN = r"^\d{10}$"


class MarketerBankDetails(BaseModel):
    """Payout destination. Never echoed back: responses carry ``bank_last4`` only."""

    model_config = ConfigDict(extra="forbid")

    bank_code: str = Field(..., min_length=1, max_length=10, description="Paystack bank code")
    account_number: str = Field(
        ..., pattern=_ACCOUNT_NUMBER_PATTERN, description="10-digit NUBAN account number"
    )
    account_name: str = Field(..., min_length=1, max_length=255)

    @field_validator("bank_code", "account_name", mode="before")
    @classmethod
    def _strip(cls, value):
        return value.strip() if isinstance(value, str) else value


class MarketerCreateRequest(MarketerBankDetails):
    name: str = Field(..., min_length=1, max_length=255)
    email: EmailStr
    code: str = Field(..., description="3-32 chars of a-z, 0-9 and hyphen; stored lowercase")

    @field_validator("name", mode="before")
    @classmethod
    def _strip_name(cls, value):
        return value.strip() if isinstance(value, str) else value

    @field_validator("code")
    @classmethod
    def _check_code(cls, value: str) -> str:
        return validate_marketer_code(value)


class MarketerBankUpdateRequest(MarketerBankDetails):
    pass


class MarketerResponse(BaseModel):
    """A marketer as the platform admin sees it. No bank code, account number or password."""

    id: UUID
    name: str
    email: str
    code: str
    status: MarketerStatus
    bank_last4: str | None = Field(None, description="Last 4 digits of the payout account")
    invite_pending: bool = Field(..., description="True until the marketer has set a password")
    created_at: datetime

    @classmethod
    def from_marketer(cls, marketer: Marketer) -> "MarketerResponse":
        account = marketer.account_number
        return cls(
            id=marketer.id,
            name=marketer.name,
            email=marketer.email,
            code=marketer.code,
            status=marketer.status,
            bank_last4=account[-4:] if account else None,
            invite_pending=marketer.password_hash is None,
            created_at=marketer.created_at,
        )


class MarketerListResponse(BaseModel):
    items: list[MarketerResponse]
    total: int
    limit: int
    offset: int
