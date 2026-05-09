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

    # ------------------------------------------------------------------
    # Revert (shared between expire and revoke)
    # ------------------------------------------------------------------

    def _revert_tenant_to_snapshot(self, grant: PromotionalGrant) -> Tenant:
        """Apply the snapshot fields back to the tenant. Caller commits."""
        tenant = self.db.query(Tenant).filter(Tenant.id == grant.tenant_id).first()
        if tenant is None:
            raise PromoGrantStateError(f"Tenant {grant.tenant_id} for grant {grant.id} not found")
        tenant.subscription_tier = grant.previous_tier
        tenant.subscription_status = grant.previous_status
        tenant.billing_cycle = grant.previous_billing_cycle
        tenant.trial_ends_at = grant.previous_trial_ends_at
        tenant.subscription_ends_at = grant.previous_subscription_ends_at
        # Recompute capabilities based on the restored tier
        self._platform_subs._update_capabilities_with_overrides(
            tenant.id, grant.previous_tier, respect_overrides=False
        )
        return tenant

    # ------------------------------------------------------------------
    # Revoke
    # ------------------------------------------------------------------

    def revoke_grant(
        self,
        *,
        grant_id: UUID,
        reason: str,
        admin_id: UUID | None,
        admin_name: str | None,
    ) -> dict:
        if not reason or len(reason) < 10:
            raise PromoGrantValidationError("reason must be at least 10 characters")

        grant = self.db.query(PromotionalGrant).filter(PromotionalGrant.id == grant_id).first()
        if grant is None:
            raise PromoGrantNotFoundError(f"Grant {grant_id} not found")
        if grant.status != "active":
            raise PromoGrantStateError(
                f"Grant {grant_id} is {grant.status!r}; only active grants can be revoked"
            )

        tenant = self._revert_tenant_to_snapshot(grant)

        grant.status = "revoked"
        grant.revoked_at = datetime.now(UTC)
        grant.revoke_reason = reason

        self.db.add(
            SubscriptionHistory(
                tenant_id=grant.tenant_id,
                action="promo_revoked",
                previous_tier=grant.granted_tier,
                new_tier=grant.previous_tier,
                previous_status="active",
                new_status=grant.previous_status,
                changed_by_type="platform_admin",
                changed_by_id=admin_id,
                changed_by_name=admin_name,
                reason=reason,
                event_metadata={
                    "grant_id": str(grant.id),
                    "campaign_code": grant.campaign_code,
                },
            )
        )

        self.db.commit()

        return {
            "grant_id": grant.id,
            "status": grant.status,
            "tenant_id": tenant.id,
            "reverted_to_tier": grant.previous_tier,
            "reverted_to_status": grant.previous_status,
        }
