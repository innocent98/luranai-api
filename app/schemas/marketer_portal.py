"""Responses of the marketer-facing API (``/api/v1/marketer/*``).

Privacy is a binding requirement: these models are the whole allow-list of what a marketer can
see. They are built field by field from query results, never from an ORM object, and forbid extra
fields so nothing can be added to a response by accident. No tenant email (only a masked form),
no plan names, no payment amounts, no conversations, no fraud flags or review reasons.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MarketerMeResponse(_Strict):
    name: str
    code: str
    link: str


ReferralStatus = Literal["signed_up", "converted", "not_converted"]


class ReferralItem(_Strict):
    company_name: str
    masked_email: str
    signed_up_at: datetime
    status: ReferralStatus
    trial_days_left: int | None
    kb_uploaded: bool
    widget_installed: bool
    converted_at: datetime | None


class ReferralTotals(_Strict):
    signed_up: int
    converted: int
    conversion_rate: float


class ReferralListResponse(_Strict):
    items: list[ReferralItem]
    totals: ReferralTotals


class PayoutHistoryItem(_Strict):
    date: datetime
    amount_kobo: int
    batch: str


class EarningsResponse(_Strict):
    this_month_earned_kobo: int
    pending_kobo: int
    payable_kobo: int
    paid_total_kobo: int
    payouts: list[PayoutHistoryItem]
