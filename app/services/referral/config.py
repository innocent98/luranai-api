"""Runtime referral settings: admin overrides in ``referral_config`` over the env defaults."""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models.referral import ReferralConfig


@dataclass(frozen=True)
class ReferralRuntimeConfig:
    reward_kobo: int
    hold_days: int
    commission_window_days: int


def get_referral_config(db: Session) -> ReferralRuntimeConfig:
    """Return the effective config: a ``referral_config`` row wins, else the env setting."""
    overrides = {
        row.key: row.value
        for row in db.execute(
            select(ReferralConfig).where(
                ReferralConfig.key.in_(("reward_kobo", "hold_days", "commission_window_days"))
            )
        ).scalars()
    }
    return ReferralRuntimeConfig(
        reward_kobo=overrides.get("reward_kobo", settings.REFERRAL_REWARD_KOBO),
        hold_days=overrides.get("hold_days", settings.REFERRAL_HOLD_DAYS),
        commission_window_days=overrides.get(
            "commission_window_days", settings.REFERRAL_COMMISSION_WINDOW_DAYS
        ),
    )
