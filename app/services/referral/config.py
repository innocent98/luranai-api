"""Runtime referral settings: admin overrides in ``referral_config`` over the env defaults."""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models.referral import ReferralConfig


CONFIG_KEYS = ("reward_kobo", "hold_days", "commission_window_days")


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
            select(ReferralConfig).where(ReferralConfig.key.in_(CONFIG_KEYS))
        ).scalars()
    }
    return ReferralRuntimeConfig(
        reward_kobo=overrides.get("reward_kobo", settings.REFERRAL_REWARD_KOBO),
        hold_days=overrides.get("hold_days", settings.REFERRAL_HOLD_DAYS),
        commission_window_days=overrides.get(
            "commission_window_days", settings.REFERRAL_COMMISSION_WINDOW_DAYS
        ),
    )


def get_config_sources(db: Session) -> dict[str, str]:
    """Where each effective value comes from: ``"db"`` (an admin override row) or ``"env"``."""
    overridden = set(
        db.execute(select(ReferralConfig.key).where(ReferralConfig.key.in_(CONFIG_KEYS)))
        .scalars()
        .all()
    )
    return {key: ("db" if key in overridden else "env") for key in CONFIG_KEYS}


def update_referral_config(db: Session, admin_id: UUID, values: dict[str, int]) -> None:
    """Upsert the given override rows in the caller's transaction (flushes, never commits).

    Only the three known keys are accepted. The values apply to rewards created afterwards;
    existing rewards keep the amount and hold date they were created with.
    """
    unknown = set(values) - set(CONFIG_KEYS)
    if unknown:
        raise ValueError(f"unknown referral config key(s): {sorted(unknown)}")
    existing = {
        row.key: row
        for row in db.execute(
            select(ReferralConfig).where(ReferralConfig.key.in_(values)).with_for_update()
        ).scalars()
    }
    for key, value in values.items():
        row = existing.get(key)
        if row is None:
            db.add(ReferralConfig(key=key, value=value, updated_by_admin_id=admin_id))
        else:
            row.value = value
            row.updated_by_admin_id = admin_id
    db.flush()
