"""Promotional grant service: lifecycle for time-bounded subscription elevations."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
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

    # ------------------------------------------------------------------
    # Expire (worker pass)
    # ------------------------------------------------------------------

    def find_grants_due_for_expiry(self) -> list[PromotionalGrant]:
        """Return all active grants whose end-time has passed."""
        now = datetime.now(UTC)
        return (
            self.db.query(PromotionalGrant)
            .filter(
                PromotionalGrant.status == "active",
                PromotionalGrant.grant_ends_at < now,
            )
            .all()
        )

    def expire_grants_due(self) -> list[UUID]:
        """Process all grants that have hit their end_at. Idempotent.

        Returns the list of grant IDs expired in this pass (so the caller
        — typically the worker — can dispatch a per-grant 'expired' email).
        """
        due = self.find_grants_due_for_expiry()
        expired_ids: list[UUID] = []
        for grant in due:
            try:
                self._revert_tenant_to_snapshot(grant)
                grant.status = "expired"
                grant.expired_at = datetime.now(UTC)
                self.db.add(SubscriptionHistory(
                    tenant_id=grant.tenant_id,
                    action="promo_expired",
                    previous_tier=grant.granted_tier,
                    new_tier=grant.previous_tier,
                    previous_status="active",
                    new_status=grant.previous_status,
                    changed_by_type="system",
                    changed_by_name="promo_lifecycle_worker",
                    reason=f"Grant ended after {grant.duration_days} days",
                    event_metadata={
                        "grant_id": str(grant.id),
                        "campaign_code": grant.campaign_code,
                    },
                ))
                self.db.commit()
                expired_ids.append(grant.id)
            except Exception:
                self.db.rollback()
                # logged at the worker layer; continue to next grant
                raise
        return expired_ids

    # ------------------------------------------------------------------
    # Convert (Paystack webhook integration point)
    # ------------------------------------------------------------------

    def mark_converted_for_tenant(
        self,
        *,
        tenant_id: UUID,
        paid_tier: str,
        paid_billing_cycle: str | None,
    ) -> PromotionalGrant | None:
        """Mark the active grant for tenant_id as converted. No-op if no active grant.

        Caller (the Paystack webhook) is expected to have already mutated the
        tenant's subscription to the paid state.
        """
        active = (
            self.db.query(PromotionalGrant)
            .filter(
                PromotionalGrant.tenant_id == tenant_id,
                PromotionalGrant.status == "active",
            )
            .first()
        )
        if active is None:
            return None
        active.status = "converted"
        active.converted_at = datetime.now(UTC)
        self.db.add(SubscriptionHistory(
            tenant_id=tenant_id,
            action="promo_converted",
            previous_tier=active.granted_tier,
            new_tier=paid_tier,
            previous_status="active",
            new_status="active",
            changed_by_type="webhook",
            changed_by_name="paystack",
            reason=f"Tenant converted to paid {paid_tier} during {active.campaign_code}",
            event_metadata={
                "grant_id": str(active.id),
                "campaign_code": active.campaign_code,
                "paid_billing_cycle": paid_billing_cycle,
            },
        ))
        self.db.commit()
        return active

    # ------------------------------------------------------------------
    # Cohort selection
    # ------------------------------------------------------------------

    def build_cohort(
        self,
        *,
        subscription_statuses: list[str],
        churned_within_days: int | None,
        exclude_already_granted: bool,
    ) -> list[Tenant]:
        if not subscription_statuses:
            return []

        query = self.db.query(Tenant).filter(Tenant.subscription_status.in_(subscription_statuses))

        if churned_within_days is not None:
            cutoff = datetime.now(UTC) - timedelta(days=churned_within_days)
            # 'churned at' is approximated by trial_ends_at for trial_expired,
            # subscription_ends_at for canceled / past_due
            query = query.filter(
                or_(
                    Tenant.trial_ends_at >= cutoff,
                    Tenant.subscription_ends_at >= cutoff,
                )
            )

        if exclude_already_granted:
            granted_subq = (
                self.db.query(PromotionalGrant.tenant_id)
                .filter(PromotionalGrant.status == "active")
                .subquery()
            )
            query = query.filter(~Tenant.id.in_(self.db.query(granted_subq.c.tenant_id)))

        return query.all()

    # ------------------------------------------------------------------
    # Preview token (signed, TTL'd cohort handle)
    # ------------------------------------------------------------------

    def encode_preview_token(
        self, tenant_ids: list[UUID], *, ttl_seconds: int = 1800
    ) -> str:
        """Sign a list of tenant IDs with SECRET_KEY + a TTL.

        The TTL is embedded in the payload so decode_preview_token can enforce
        the same expiry without the caller having to re-specify it.

        Returns a URL-safe token.
        """
        from itsdangerous import TimestampSigner

        from app.core.config import settings

        signer = TimestampSigner(settings.SECRET_KEY, salt="promo-preview")
        ids_str = ",".join(str(tid) for tid in tenant_ids)
        # Format: "<ttl_seconds>|<id1>,<id2>,..."  — TTL is stored so decode can enforce it
        payload = f"{ttl_seconds}|{ids_str}"
        return signer.sign(payload).decode("utf-8")

    def decode_preview_token(self, token: str, *, ttl_seconds: int | None = None) -> list[UUID]:
        from itsdangerous import BadSignature, SignatureExpired, TimestampSigner

        from app.core.config import settings

        signer = TimestampSigner(settings.SECRET_KEY, salt="promo-preview")
        try:
            raw = signer.unsign(token, max_age=None).decode("utf-8")
        except BadSignature as e:
            raise PromoGrantValidationError("preview_token invalid") from e

        # Parse the embedded TTL and IDs
        try:
            ttl_str, ids_str = raw.split("|", 1)
            effective_ttl = ttl_seconds if ttl_seconds is not None else int(ttl_str)
        except ValueError as e:
            raise PromoGrantValidationError("preview_token malformed") from e

        # Re-verify with the correct max_age now that we have the embedded TTL
        try:
            signer.unsign(token, max_age=effective_ttl)
        except SignatureExpired as e:
            raise PromoGrantValidationError("preview_token expired") from e
        except BadSignature as e:
            raise PromoGrantValidationError("preview_token invalid") from e

        return [UUID(s) for s in ids_str.split(",") if s]

    # ------------------------------------------------------------------
    # Campaign aggregates
    # ------------------------------------------------------------------

    def list_campaigns_with_stats(self) -> list[dict]:
        """Aggregate stats per campaign code.

        Tables stay small for v1 — this is a simple per-campaign group + tally.
        """
        all_grants = self.db.query(PromotionalGrant).order_by(PromotionalGrant.created_at).all()
        by_campaign: dict[str, list[PromotionalGrant]] = {}
        for grant in all_grants:
            by_campaign.setdefault(grant.campaign_code, []).append(grant)

        campaigns = []
        for code, grants in by_campaign.items():
            head = grants[0]
            stats = {
                "total": len(grants),
                "active": sum(1 for g in grants if g.status == "active"),
                "converted": sum(1 for g in grants if g.status == "converted"),
                "expired": sum(1 for g in grants if g.status == "expired"),
                "revoked": sum(1 for g in grants if g.status == "revoked"),
                "by_source": {
                    "admin_bulk": sum(1 for g in grants if g.source == "admin_bulk"),
                    "admin_single": sum(1 for g in grants if g.source == "admin_single"),
                    "auto_signup": sum(1 for g in grants if g.source == "auto_signup"),
                },
            }
            campaigns.append({
                "campaign_code": code,
                "campaign_name": head.campaign_name,
                "granted_tier": head.granted_tier,
                "duration_days": head.duration_days,
                "first_grant_at": head.created_at,
                "stats": stats,
            })
        campaigns.sort(key=lambda c: c["first_grant_at"], reverse=True)
        return campaigns

    # ------------------------------------------------------------------
    # Bulk create
    # ------------------------------------------------------------------

    def bulk_create_grants(
        self,
        *,
        tenant_ids: list[UUID],
        campaign_code: str,
        campaign_name: str | None,
        granted_tier: str,
        duration_days: int,
        granted_by_admin_id: UUID,
        granted_by_admin_name: str,
        reason: str,
        force_replace_active: bool,
    ) -> dict:
        granted = 0
        skipped = 0
        failed = 0
        sample_ids: list[UUID] = []

        for tenant_id in tenant_ids:
            # Use a nested transaction (SAVEPOINT) per tenant so that a rollback
            # on IntegrityError only undoes the failed attempt, not prior successes.
            sp = self.db.begin_nested()
            try:
                if force_replace_active:
                    # Revoke any existing active grant first (different campaign)
                    existing = (
                        self.db.query(PromotionalGrant)
                        .filter(
                            PromotionalGrant.tenant_id == tenant_id,
                            PromotionalGrant.status == "active",
                        )
                        .first()
                    )
                    if existing is not None:
                        self.revoke_grant(
                            grant_id=existing.id,
                            reason=f"Replaced by bulk grant from campaign {campaign_code}",
                            admin_id=granted_by_admin_id,
                            admin_name=granted_by_admin_name,
                        )
                grant = self.create_grant(
                    tenant_id=tenant_id,
                    campaign_code=campaign_code,
                    campaign_name=campaign_name,
                    granted_tier=granted_tier,
                    duration_days=duration_days,
                    source="admin_bulk",
                    granted_by_admin_id=granted_by_admin_id,
                    granted_by_admin_name=granted_by_admin_name,
                    reason=reason,
                )
                # create_grant calls db.commit() internally, which releases the
                # savepoint; no explicit sp.commit() needed here.
                granted += 1
                if len(sample_ids) < 10:
                    sample_ids.append(grant.id)
            except IntegrityError:
                sp.rollback()
                skipped += 1
            except (PromoGrantValidationError, PromoGrantNotFoundError):
                sp.rollback()
                failed += 1

        return {
            "granted": granted,
            "skipped_already_granted": skipped,
            "failed": failed,
            "grant_ids_sample": sample_ids,
        }
