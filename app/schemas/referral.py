"""Referral program schemas (platform-admin marketer and payout-batch management)."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

from app.db.models.referral import BatchStatus, Marketer, MarketerStatus, PayoutItemStatus
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


# --- payout batches -----------------------------------------------------------------------------


class PayoutItemCounts(BaseModel):
    """How many items of a batch are in each state (every state is always present)."""

    pending: int = 0
    processing: int = 0
    paid: int = 0
    failed: int = 0
    skipped: int = 0
    awaiting_manual: int = 0


class PayoutBatchSummary(BaseModel):
    id: UUID
    period_label: str
    status: BatchStatus
    total_kobo: int
    item_counts: PayoutItemCounts
    approved_at: datetime | None
    approved_by_admin_id: UUID | None
    created_at: datetime


class PayoutBatchListResponse(BaseModel):
    items: list[PayoutBatchSummary]
    total: int
    limit: int
    offset: int


class PayoutItemResponse(BaseModel):
    """One marketer's line. Bank details are never included: ``bank_last4`` only."""

    id: UUID
    marketer_id: UUID
    marketer_name: str
    marketer_code: str
    amount_kobo: int
    status: PayoutItemStatus
    failure_reason: str | None
    provider_status: str | None
    attempt: int
    transfer_reference: str | None = Field(
        None, description="Paystack reference, or the bank reference once marked paid manually"
    )
    reward_count: int
    reward_ids: list[UUID]
    bank_last4: str | None
    held_rewards_excluded: int = Field(
        ..., description="This marketer's held rewards that are not part of this batch"
    )


class PayoutBatchDetail(PayoutBatchSummary):
    items: list[PayoutItemResponse]


class MarkPaidEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    marketer_id: UUID
    bank_reference: str = Field(..., min_length=1, max_length=100)

    @field_validator("bank_reference", mode="before")
    @classmethod
    def _strip(cls, value):
        return value.strip() if isinstance(value, str) else value


class MarkPaidRequest(BaseModel):
    """Record payments an admin made outside the system (R34)."""

    model_config = ConfigDict(extra="forbid")

    items: list[MarkPaidEntry] = Field(..., min_length=1)

    @model_validator(mode="after")
    def _no_duplicates(self) -> "MarkPaidRequest":
        marketers = [entry.marketer_id for entry in self.items]
        references = [entry.bank_reference for entry in self.items]
        if len(set(marketers)) != len(marketers):
            raise ValueError("each marketer may appear only once")
        if len(set(references)) != len(references):
            raise ValueError("each payment needs its own bank reference")
        return self
