"""Marketer-facing API: the referral dashboard of a signed-in marketer.

Every route depends on ``get_current_marketer`` (marketer cookie only) and is a read-only GET, so
there is nothing for CSRF to attack. The dependency returns the ORM ``Marketer`` (password hash,
encrypted bank fields); it is never serialised: the services build the allow-list response models
field by field. The marketer sees only their own data, and only what the design allows (code,
link, referred tenants with a masked email, totals, earnings, payout history).
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_current_marketer, get_db
from app.db.models.referral import Marketer
from app.schemas.marketer_portal import (
    EarningsResponse,
    MarketerMeResponse,
    ReferralListResponse,
)
from app.services.referral import marketer_views

router = APIRouter()


@router.get("/me", response_model=MarketerMeResponse)
def get_me(marketer: Marketer = Depends(get_current_marketer)):
    """Name, referral code and the shareable signup link."""
    return marketer_views.get_me(marketer)


@router.get("/referrals", response_model=ReferralListResponse)
def list_referrals(
    limit: int = Query(200, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    marketer: Marketer = Depends(get_current_marketer),
):
    """Referred tenants, newest first (company name, masked email, status, activation hints),
    with totals over all of them. Self-referrals are not shown."""
    return marketer_views.list_referrals(db, marketer, limit, offset)


@router.get("/earnings", response_model=EarningsResponse)
def get_earnings(
    db: Session = Depends(get_db),
    marketer: Marketer = Depends(get_current_marketer),
):
    """This month's earnings, pending / payable / paid totals (kobo) and the payout history."""
    return marketer_views.get_earnings(db, marketer)
