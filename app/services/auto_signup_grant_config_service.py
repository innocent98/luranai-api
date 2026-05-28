"""Singleton service managing the auto-signup grant configuration."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.orm import Session

from app.db.models.promotional_grant import AutoSignupGrantConfig


class AutoSignupConfigValidationError(ValueError):
    """Raised when the config update is internally inconsistent."""


class AutoSignupGrantConfigService:
    """CRUD on the singleton auto_signup_grant_config row."""

    def __init__(self, db: Session):
        self.db = db

    def get(self) -> AutoSignupGrantConfig:
        """Return the singleton row. Migration seeds it; we should always find one."""
        row = self.db.query(AutoSignupGrantConfig).first()
        if row is None:
            raise RuntimeError(
                "auto_signup_grant_config singleton not seeded. " "Run alembic upgrade head."
            )
        return row

    def is_active(self) -> bool:
        """True iff config.enabled is true AND valid_until has not passed."""
        row = self.get()
        if not row.enabled:
            return False
        if row.valid_until is None:
            return True
        return datetime.now(UTC) < row.valid_until

    def update(
        self,
        *,
        enabled: bool,
        campaign_code: str | None,
        campaign_name: str | None,
        granted_tier: str | None,
        duration_days: int | None,
        valid_until: datetime | None,
        admin_id: UUID | None,
        admin_name: str | None,
    ) -> AutoSignupGrantConfig:
        if enabled:
            missing = [
                name
                for name, val in (
                    ("campaign_code", campaign_code),
                    ("granted_tier", granted_tier),
                    ("duration_days", duration_days),
                )
                if val is None
            ]
            if missing:
                raise AutoSignupConfigValidationError(
                    f"Cannot enable auto-signup with missing fields: {', '.join(missing)}"
                )

        row = self.get()
        row.enabled = enabled
        row.campaign_code = campaign_code
        row.campaign_name = campaign_name
        row.granted_tier = granted_tier
        row.duration_days = duration_days
        row.valid_until = valid_until
        row.updated_at = datetime.now(UTC)
        row.updated_by_admin_id = admin_id
        row.updated_by_admin_name = admin_name
        self.db.commit()
        self.db.refresh(row)
        return row
