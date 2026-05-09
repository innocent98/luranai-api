"""Promotional grant service: lifecycle for time-bounded subscription elevations."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy.orm import Session

from app.db.models.promotional_grant import PromotionalGrant
from app.db.models.subscription import SubscriptionHistory
from app.db.models.tenant import Tenant
from app.services.platform_subscription_service import PlatformSubscriptionService

ALLOWED_TIERS = {"lite", "growth", "professional"}
ALLOWED_SOURCES = {"admin_bulk", "admin_single", "auto_signup"}
DURATION_MIN_DAYS = 14
DURATION_MAX_DAYS = 180


class PromoGrantValidationError(ValueError):
    """Validation failure on input parameters (not DB-level constraints)."""


class PromoGrantNotFoundError(LookupError):
    """Grant lookup failed."""


class PromoGrantStateError(RuntimeError):
    """Grant is in a state incompatible with the requested operation."""


class PromotionalGrantService:
    def __init__(self, db: Session):
        self.db = db
        # Reuse the existing capability-recompute path; this is the single source
        # of truth for "tier → capabilities" already used by override.
        self._platform_subs = PlatformSubscriptionService(db)

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    def create_grant(
        self,
        *,
        tenant_id: UUID,
        campaign_code: str,
        campaign_name: str | None,
        granted_tier: str,
        duration_days: int,
        source: str,
        granted_by_admin_id: UUID | None,
        granted_by_admin_name: str | None,
        reason: str,
    ) -> PromotionalGrant:
        if granted_tier not in ALLOWED_TIERS:
            raise PromoGrantValidationError(
                f"granted_tier must be one of {sorted(ALLOWED_TIERS)}; got {granted_tier!r}"
            )
        if source not in ALLOWED_SOURCES:
            raise PromoGrantValidationError(
                f"source must be one of {sorted(ALLOWED_SOURCES)}; got {source!r}"
            )
        if duration_days < DURATION_MIN_DAYS or duration_days > DURATION_MAX_DAYS:
            raise PromoGrantValidationError(
                f"duration_days must be in [{DURATION_MIN_DAYS}, {DURATION_MAX_DAYS}]; "
                f"got {duration_days}"
            )
        if not reason or len(reason) < 10:
            raise PromoGrantValidationError("reason must be at least 10 characters")

        tenant = self.db.query(Tenant).filter(Tenant.id == tenant_id).first()
        if tenant is None:
            raise PromoGrantNotFoundError(f"Tenant {tenant_id} not found")

        now = datetime.now(UTC)
        starts_at = now
        ends_at = now + timedelta(days=duration_days)

        # Snapshot the pre-grant state.
        if source == "auto_signup":
            # Synthetic snapshot: revert puts them straight at trial_expired.
            previous_tier = "trial"
            previous_status = "trial_expired"
            previous_billing_cycle = None
            previous_trial_ends_at = now
            previous_subscription_ends_at = None
        else:
            previous_tier = tenant.subscription_tier
            previous_status = tenant.subscription_status
            previous_billing_cycle = tenant.billing_cycle
            previous_trial_ends_at = tenant.trial_ends_at
            previous_subscription_ends_at = tenant.subscription_ends_at

        grant = PromotionalGrant(
            tenant_id=tenant_id,
            campaign_code=campaign_code,
            campaign_name=campaign_name,
            granted_tier=granted_tier,
            duration_days=duration_days,
            grant_starts_at=starts_at,
            grant_ends_at=ends_at,
            previous_tier=previous_tier,
            previous_status=previous_status,
            previous_billing_cycle=previous_billing_cycle,
            previous_trial_ends_at=previous_trial_ends_at,
            previous_subscription_ends_at=previous_subscription_ends_at,
            status="active",
            source=source,
            granted_by_admin_id=granted_by_admin_id,
            granted_by_admin_name=granted_by_admin_name,
            reason=reason,
        )
        self.db.add(grant)
        self.db.flush()  # surfaces partial-unique-index violations as IntegrityError before mutating tenant

        # Apply elevation
        tenant.subscription_tier = granted_tier
        tenant.subscription_status = "active"
        tenant.billing_cycle = None  # promotional grants are not billed
        tenant.subscription_starts_at = starts_at
        tenant.subscription_ends_at = ends_at
        tenant.trial_ends_at = None  # don't double-count trial during a grant

        # Recompute capabilities (reset overrides; grants apply tier defaults)
        self._platform_subs._update_capabilities_with_overrides(
            tenant_id, granted_tier, respect_overrides=False
        )

        self.db.add(
            SubscriptionHistory(
                tenant_id=tenant_id,
                action="promo_granted",
                previous_tier=previous_tier,
                new_tier=granted_tier,
                previous_status=previous_status,
                new_status="active",
                changed_by_type="platform_admin" if source != "auto_signup" else "system",
                changed_by_id=granted_by_admin_id,
                changed_by_name=granted_by_admin_name,
                reason=reason,
                event_metadata={
                    "grant_id": str(grant.id),
                    "campaign_code": campaign_code,
                    "source": source,
                    "duration_days": duration_days,
                },
            )
        )

        self.db.commit()
        self.db.refresh(grant)
        return grant
