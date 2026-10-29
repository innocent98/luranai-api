"""What a marketer may see of their own referrals and earnings.

This module is the privacy boundary of the marketer API (design spec 5.3 / 5.9). It exposes only:
company name, masked signup email, signup date, a status, trial days left, two activation hints,
totals, earnings and payout history. It never returns or reads plan names, payment amounts,
conversations, review reasons or fraud flags, and every result is built field by field into the
allow-list models of ``app.schemas.marketer_portal``.

Status of a referred tenant (spec 5.4, rulings R44 / R45):

* a reward that is not ``void`` (pending, held, payable, batched, paid) means ``converted``, with
  ``converted_at`` from the reward; a held reward is indistinguishable from any other here;
* otherwise ``signed_up`` while the trial is running (the tenant is still ``trialing`` and its
  trial end is ahead) and the commission window is open, with ``trial_days_left`` rounded up;
* otherwise ``not_converted``;
* attributions flagged ``self_referral`` are hidden from the list and from the totals.

The commission window is measured from the tenant's signup with the *current* configured length.
"""

import math
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models.referral import (
    Marketer,
    PayoutBatch,
    PayoutItem,
    PayoutItemStatus,
    ReferralAttribution,
    ReferralReward,
    RewardStatus,
)
from app.db.models.tenant import Tenant
from app.db.models.user import User
from app.schemas.marketer_portal import (
    EarningsResponse,
    MarketerMeResponse,
    PayoutHistoryItem,
    ReferralItem,
    ReferralListResponse,
    ReferralTotals,
)
from app.services.onboarding_service import get_onboarding_status
from app.services.referral.config import get_referral_config
from app.services.referral.masking import mask_email

LAGOS = ZoneInfo("Africa/Lagos")
PAYOUT_HISTORY_LIMIT = 50
_DAY = timedelta(days=1)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _now(now: datetime | None) -> datetime:
    return _aware(now) if now else datetime.now(UTC)


def get_me(marketer: Marketer) -> MarketerMeResponse:
    base = settings.REFERRAL_LINK_BASE_URL.rstrip("/")
    return MarketerMeResponse(
        name=marketer.name, code=marketer.code, link=f"{base}/register?ref={marketer.code}"
    )


def _totals(db: Session, marketer_id) -> ReferralTotals:
    visible = (
        ReferralAttribution.marketer_id == marketer_id,
        ReferralAttribution.self_referral.is_(False),
    )
    signed_up = db.scalar(select(func.count()).select_from(ReferralAttribution).where(*visible))
    converted = db.scalar(
        select(func.count())
        .select_from(ReferralAttribution)
        .join(ReferralReward, ReferralReward.tenant_id == ReferralAttribution.tenant_id)
        .where(
            *visible,
            ReferralReward.marketer_id == marketer_id,
            ReferralReward.status != RewardStatus.VOID,
        )
    )
    signed_up, converted = signed_up or 0, converted or 0
    rate = round(converted / signed_up, 4) if signed_up else 0.0
    return ReferralTotals(signed_up=signed_up, converted=converted, conversion_rate=rate)


def _owner_emails(db: Session, tenant_ids: list) -> dict:
    """The signup email of each tenant: its earliest-created user (one batched query)."""
    if not tenant_ids:
        return {}
    ranked = (
        select(
            User.id.label("user_id"),
            User.tenant_id.label("tenant_id"),
            func.row_number()
            .over(partition_by=User.tenant_id, order_by=(User.created_at, User.id))
            .label("rank"),
        )
        .where(User.tenant_id.in_(tenant_ids))
        .subquery()
    )
    rows = db.execute(
        select(ranked.c.tenant_id, User)
        .select_from(ranked)
        .join(User, User.id == ranked.c.user_id)
        .where(ranked.c.rank == 1)
    ).all()
    return {tenant_id: user.email for tenant_id, user in rows}


def _activation_hints(db: Session, tenant_id) -> tuple[bool, bool]:
    """(knowledge base uploaded, widget installed): the tenant's own onboarding answers."""
    try:
        # A SAVEPOINT, so a database error in the onboarding code cannot poison the outer
        # transaction (the rest of the page and the totals still run).
        with db.begin_nested():
            steps = get_onboarding_status(db, tenant_id).steps
        return bool(steps[0].done), bool(steps[1].done)
    except Exception as exc:  # a hint must never break the page; class and id only
        logger.warning(f"activation hints unavailable for tenant {tenant_id}: {type(exc).__name__}")
        return False, False


def list_referrals(
    db: Session,
    marketer: Marketer,
    limit: int = 50,
    offset: int = 0,
    *,
    now: datetime | None = None,
) -> ReferralListResponse:
    """The marketer's referred tenants, newest signup first, with totals over all of them."""
    moment = _now(now)
    window = timedelta(days=get_referral_config(db).commission_window_days)
    rows = db.execute(
        select(ReferralAttribution, Tenant)
        .join(Tenant, Tenant.id == ReferralAttribution.tenant_id)
        .where(
            ReferralAttribution.marketer_id == marketer.id,
            ReferralAttribution.self_referral.is_(False),
        )
        .order_by(Tenant.created_at.desc(), ReferralAttribution.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    tenant_ids = [tenant.id for _, tenant in rows]

    rewards = {}
    if tenant_ids:
        rewards = {
            r.tenant_id: r
            for r in db.scalars(
                select(ReferralReward).where(
                    ReferralReward.tenant_id.in_(tenant_ids),
                    ReferralReward.marketer_id == marketer.id,
                    ReferralReward.status != RewardStatus.VOID,
                )
            )
        }
    emails = _owner_emails(db, tenant_ids)

    items = []
    for _, tenant in rows:
        reward = rewards.get(tenant.id)
        trial_days_left: int | None = None
        converted_at: datetime | None = None
        if reward is not None:
            status = "converted"
            converted_at = reward.converted_at
        else:
            trial_end = _aware(tenant.trial_ends_at) if tenant.trial_ends_at else None
            in_trial = (
                tenant.subscription_status == "trialing"
                and trial_end is not None
                and trial_end > moment
            )
            window_open = _aware(tenant.created_at) + window > moment
            if in_trial and window_open:
                status = "signed_up"
                trial_days_left = max(0, math.ceil((trial_end - moment) / _DAY))
            else:
                status = "not_converted"
        kb_uploaded = widget_installed = False
        if status == "signed_up":
            kb_uploaded, widget_installed = _activation_hints(db, tenant.id)
        items.append(
            ReferralItem(
                company_name=tenant.display_name,
                masked_email=mask_email(emails.get(tenant.id, "")),
                signed_up_at=tenant.created_at,
                status=status,
                trial_days_left=trial_days_left,
                kb_uploaded=kb_uploaded,
                widget_installed=widget_installed,
                converted_at=converted_at,
            )
        )
    return ReferralListResponse(items=items, totals=_totals(db, marketer.id))


def _lagos_month_bounds_utc(moment: datetime) -> tuple[datetime, datetime]:
    """[start, end) of the calendar month containing ``moment`` in Africa/Lagos, as UTC."""
    local = moment.astimezone(LAGOS)
    year, month = local.year, local.month
    next_year, next_month = (year + 1, 1) if month == 12 else (year, month + 1)
    start = datetime(year, month, 1, tzinfo=LAGOS)
    end = datetime(next_year, next_month, 1, tzinfo=LAGOS)
    return start.astimezone(UTC), end.astimezone(UTC)


def get_earnings(
    db: Session, marketer: Marketer, *, now: datetime | None = None
) -> EarningsResponse:
    """Money buckets and the paid payout history (newest first, at most 50 entries).

    Held rewards count into ``pending_kobo`` together with pending ones; nothing tells the
    marketer why a reward is waiting.
    """
    moment = _now(now)
    by_status = dict(
        db.execute(
            select(ReferralReward.status, func.coalesce(func.sum(ReferralReward.amount_kobo), 0))
            .where(ReferralReward.marketer_id == marketer.id)
            .group_by(ReferralReward.status)
        ).all()
    )

    def total(*statuses: RewardStatus) -> int:
        return int(sum(by_status.get(s, 0) for s in statuses))

    start, end = _lagos_month_bounds_utc(moment)
    this_month = db.scalar(
        select(func.coalesce(func.sum(ReferralReward.amount_kobo), 0)).where(
            ReferralReward.marketer_id == marketer.id,
            ReferralReward.status != RewardStatus.VOID,
            ReferralReward.converted_at >= start,
            ReferralReward.converted_at < end,
        )
    )
    paid_items = db.execute(
        select(PayoutItem.paid_at, PayoutItem.amount_kobo, PayoutBatch.period_label)
        .join(PayoutBatch, PayoutBatch.id == PayoutItem.batch_id)
        .where(
            PayoutItem.marketer_id == marketer.id,
            PayoutItem.status == PayoutItemStatus.PAID,
            PayoutItem.paid_at.is_not(None),
        )
        .order_by(PayoutItem.paid_at.desc(), PayoutItem.id.desc())
        .limit(PAYOUT_HISTORY_LIMIT)
    ).all()
    return EarningsResponse(
        this_month_earned_kobo=int(this_month or 0),
        pending_kobo=total(RewardStatus.PENDING, RewardStatus.HELD),
        payable_kobo=total(RewardStatus.PAYABLE, RewardStatus.BATCHED),
        paid_total_kobo=total(RewardStatus.PAID),
        payouts=[
            PayoutHistoryItem(date=paid_at, amount_kobo=amount, batch=label)
            for paid_at, amount, label in paid_items
        ],
    )
