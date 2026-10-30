"""Offerings settings, the "everything is still correct today" confirm, and the Not-found list.

`GET` never writes: a tenant with no settings row sees the defaults (currency from the sales agent
config, else NGN). The row is created on the first write, under the same lock that hands out `#seq`.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.offering import OfferingMiss, TenantOfferingsSettings
from app.services.offering_service import (
    OfferingError,
    OfferingService,
    validation_error,
)

MISS_RETENTION_DAYS = 90
DEFAULT_MISS_WINDOW_DAYS = 30
_CURRENCY = re.compile(r"^[A-Za-z]{3}$")


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


class OfferingSettingsService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.offerings = OfferingService(db)

    @staticmethod
    def _to_dict(row: TenantOfferingsSettings) -> dict[str, Any]:
        return {
            "enabled": row.enabled,
            "currency": row.currency,
            "stale_after_days": row.stale_after_days,
            "confirmed_at": _iso(row.confirmed_at),
        }

    def get(self, tenant_id: UUID) -> dict[str, Any]:
        row = self.db.execute(
            select(TenantOfferingsSettings).where(TenantOfferingsSettings.tenant_id == tenant_id)
        ).scalar_one_or_none()
        if row is None:
            return {
                "enabled": True,
                "currency": self.offerings.default_currency(tenant_id),
                "stale_after_days": 7,
                "confirmed_at": None,
            }
        return self._to_dict(row)

    def patch(self, tenant_id: UUID, changes: dict[str, Any]) -> dict[str, Any]:
        self.offerings.require_write(tenant_id)
        errors: list[dict[str, str]] = []
        days = changes.get("stale_after_days")
        if days is not None and not 0 <= days <= 365:
            errors.append({"field": "stale_after_days", "code": "stale_after_days_invalid"})
        currency = changes.get("currency")
        if currency is not None and not _CURRENCY.match(currency.strip()):
            errors.append({"field": "currency", "code": "currency_invalid"})
        if errors:
            raise validation_error(errors)
        row = self.offerings.lock_settings(tenant_id)
        if changes.get("enabled") is not None:
            row.enabled = bool(changes["enabled"])
        if days is not None:
            row.stale_after_days = days
        if currency is not None:
            row.currency = currency.strip().upper()
        self.db.commit()
        self.db.refresh(row)
        return self._to_dict(row)

    def confirm(self, tenant_id: UUID) -> dict[str, Any]:
        """The owner says the list is still correct today: resets the staleness caveat."""
        self.offerings.require_write(tenant_id)
        row = self.offerings.lock_settings(tenant_id)
        row.confirmed_at = datetime.now(UTC)
        self.offerings.record_activity(tenant_id)
        self.db.commit()
        self.db.refresh(row)
        return {"confirmed_at": _iso(row.confirmed_at)}

    # -- Not-found list ------------------------------------------------------------------

    def list_misses(self, tenant_id: UUID, days: int = DEFAULT_MISS_WINDOW_DAYS) -> list[dict]:
        since = datetime.now(UTC) - timedelta(days=days)
        rows = self.db.execute(
            select(OfferingMiss)
            .where(OfferingMiss.tenant_id == tenant_id, OfferingMiss.last_seen_at >= since)
            .order_by(OfferingMiss.count.desc(), OfferingMiss.last_seen_at.desc())
        ).scalars()
        return [
            {
                "id": str(m.id),
                "term": m.term,
                "count": m.count,
                "last_seen_at": _iso(m.last_seen_at),
            }
            for m in rows
        ]

    def dismiss_miss(self, tenant_id: UUID, miss_id: UUID) -> None:
        self.offerings.require_write(tenant_id)
        row = self.db.execute(
            select(OfferingMiss).where(
                OfferingMiss.id == miss_id, OfferingMiss.tenant_id == tenant_id
            )
        ).scalar_one_or_none()
        if row is None:
            raise OfferingError(404, "offering_miss_not_found", "Not-found term not found")
        self.db.delete(row)
        self.db.commit()
