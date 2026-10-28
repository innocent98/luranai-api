"""Entitlements: the one place that decides what a tenant's plan allows.

Every gate (HTTP endpoints, the chat pipeline, the notification worker, the WhatsApp service) and
`GET /tenant/entitlements` call `get_entitlements`. Resolution order per capability:

1. the plan row of the tenant's effective tier (an active promotional grant's tier wins over the
   tenant's own tier; the grant service also writes the tier onto the tenant, so they agree),
2. then the tenant's `override_flags`: a field a platform admin edited by hand keeps its stored
   value in `tenant_capabilities`,
3. a tenant without a plan row (empty plans table, unknown tier) falls back to its stored
   capability values; with neither, everything is off.

The stored `tenant_capabilities` columns are a synced copy of the plan (see
`apply_plan_to_capabilities`) kept for the code paths that read a single row; reading through the
resolver means a stale copy cannot widen or narrow a plan.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, null, select
from sqlalchemy.orm import Session, object_session

from app.db.models.subscription_plan import PLAN_ONLY_CAPABILITY_FIELDS, SubscriptionPlan
from app.db.models.tenant import Tenant, TenantCapabilities
from app.services.plan_catalog import TIER_ORDER, plan_capabilities

# Display names for the gate message when the plan row cannot be read.
_TIER_NAMES = {
    "trial": "Trial",
    "lite": "Lite",
    "professional": "Professional",
    "growth": "Growth",
    "enterprise": "Enterprise",
}


LAPSED_STATUSES = frozenset({"trial_expired", "canceled", "past_due"})


@dataclass(frozen=True)
class FeatureDef:
    """How one entitlement key maps to capability columns."""

    key: str
    label: str
    flag: str | None = None  # plan-driven on/off column
    limit: str | None = None  # plan-driven numeric column (None = unlimited)
    grant: str | None = None  # tenant-only platform grant column (never copied from the plan)


FEATURES: dict[str, FeatureDef] = {
    f.key: f
    for f in (
        FeatureDef("sales_close", "meeting booking and quotes", flag="can_use_sales_close"),
        FeatureDef(
            "crm_notifications",
            "Slack and CRM notifications",
            flag="can_use_crm_notifications",
        ),
        FeatureDef("screen_recording", "screen recording", flag="can_use_screen_recording"),
        FeatureDef(
            "ratings_export", "the ratings breakdown and CSV export", flag="can_export_ratings"
        ),
        FeatureDef("lookup_chains", "lookup chains", flag="can_use_lookup_chains"),
        FeatureDef("webhooks", "webhook flow nodes", flag="can_use_webhooks"),
        FeatureDef("analytics", "analytics", flag="can_access_analytics"),
        FeatureDef(
            "ai_actions",
            "AI actions",
            flag="can_use_ai_chat_automation",
            limit="max_automation_actions",
        ),
        FeatureDef("whatsapp", "WhatsApp", limit="max_whatsapp_numbers", grant="can_use_whatsapp"),
        FeatureDef("voice", "voice chat", grant="can_use_voice"),
    )
}


def plan_capability_fields() -> tuple[str, ...]:
    """Plan columns that are mirrored onto `tenant_capabilities` (and synced on a tier change)."""
    return tuple(
        name
        for name in SubscriptionPlan.capability_columns()
        if name not in PLAN_ONLY_CAPABILITY_FIELDS
    )


class FeatureNotInPlan(Exception):
    """The tenant's plan does not include `feature`. Rendered as the flat 403 body below."""

    status_code = 403
    code = "feature_not_in_plan"

    def __init__(self, feature: str, min_tier: str | None, message: str) -> None:
        super().__init__(message)
        self.feature = feature
        self.min_tier = min_tier
        self.message = message

    def body(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "feature": self.feature,
            "min_tier": self.min_tier,
            "message": self.message,
        }


def _grant_tier(db: Session, tenant_id: UUID) -> str | None:
    """Tier granted by the tenant's active promotional grant, if any.

    The grant service writes the granted tier onto the tenant and an expiry worker reverts it
    later; between the end date and that worker run the grant is over, so resolve with the tier the
    tenant had before it instead of the granted one.
    """
    from app.db.models.promotional_grant import PromotionalGrant

    row = db.execute(
        select(
            PromotionalGrant.granted_tier,
            PromotionalGrant.previous_tier,
            PromotionalGrant.grant_ends_at,
        )
        .where(PromotionalGrant.tenant_id == tenant_id, PromotionalGrant.status == "active")
        .order_by(PromotionalGrant.grant_starts_at.desc())
        .limit(1)
    ).first()
    if row is None:
        return None
    ends_at = row.grant_ends_at
    if ends_at is not None and ends_at.tzinfo is None:
        ends_at = ends_at.replace(tzinfo=UTC)
    return (
        row.previous_tier
        if ends_at is not None and ends_at <= datetime.now(UTC)
        else row.granted_tier
    )


def _tier_values(db: Session, tier: str) -> dict[str, Any] | None:
    """Capability values of a tier from its plan row, or None when there is no such row."""
    plan = db.execute(
        select(SubscriptionPlan).where(SubscriptionPlan.tier == tier)
    ).scalar_one_or_none()
    return plan.get_capabilities() if plan is not None else None


class Entitlements:
    """A tenant's resolved capabilities. Build with `get_entitlements`."""

    def __init__(
        self,
        db: Session,
        tenant_id: UUID,
        tier: str,
        subscription_status: str,
        values: dict[str, Any],
    ) -> None:
        self._db = db
        self.tenant_id = tenant_id
        self.tier = tier
        self.subscription_status = subscription_status
        self.values = values
        # Same statuses the staff API answers 402 for: a lapsed account keeps its tier but none of
        # the features, so routes without the 402 check (ratings, sales config, public meeting
        # endpoints) cannot be used for free after the trial or a cancellation.
        self.lapsed = subscription_status in LAPSED_STATUSES

    def value(self, field: str) -> Any:
        return self.values.get(field)

    def limit(self, key: str) -> int | None:
        """Numeric limit of a feature; None = unlimited."""
        feature = FEATURES[key]
        assert feature.limit is not None, f"{key} has no limit"
        return self.values.get(feature.limit)

    def enabled(self, key: str) -> bool:
        if self.lapsed:
            return False
        feature = FEATURES[key]
        if feature.grant is not None and not self.values.get(feature.grant):
            return False
        if feature.flag is not None and not self.values.get(feature.flag):
            return False
        if feature.limit is not None:
            limit = self.values.get(feature.limit)
            if limit is not None and limit <= 0:
                return False
        return True

    def min_tier(self, key: str) -> str | None:
        return min_tier_for(self._db, key)

    def require(self, key: str) -> None:
        """Raise FeatureNotInPlan unless the feature is on for this tenant."""
        if not self.enabled(key):
            min_tier = self.min_tier(key)
            raise FeatureNotInPlan(key, min_tier, _gate_message(key, min_tier))

    def usage(self, key: str) -> int | None:
        """Current consumption for limited features; None for the rest."""
        if key == "ai_actions":
            from app.db.models.automation import TenantAction

            return int(
                self._db.execute(
                    select(func.count())
                    .select_from(TenantAction)
                    .where(TenantAction.tenant_id == self.tenant_id)
                ).scalar_one()
            )
        if key == "whatsapp":
            from app.db.models.whatsapp import WhatsAppConnection

            return int(
                self._db.execute(
                    select(func.count())
                    .select_from(WhatsAppConnection)
                    .where(WhatsAppConnection.tenant_id == self.tenant_id)
                ).scalar_one()
            )
        return None

    def to_response(self) -> dict[str, Any]:
        """The `GET /tenant/entitlements` body."""
        features: dict[str, Any] = {}
        for key, feature in FEATURES.items():
            entry: dict[str, Any] = {"enabled": self.enabled(key)}
            if feature.limit is not None:
                entry["limit"] = self.limit(key)
                entry["used"] = self.usage(key)
            entry["min_tier"] = self.min_tier(key)
            features[key] = entry
        return {
            "tier": self.tier,
            "subscription_status": self.subscription_status,
            "features": features,
        }


def get_entitlements(tenant: Tenant, db: Session | None = None) -> Entitlements:
    """Resolve what `tenant` may use. The only function gates should ask."""
    db = db or object_session(tenant)
    if db is None:
        raise RuntimeError("get_entitlements needs a session (tenant is detached)")

    tier = str(tenant.subscription_tier)
    grant_tier = _grant_tier(db, tenant.id)
    if grant_tier in TIER_ORDER:
        tier = grant_tier

    plan_values = _tier_values(db, tier)
    caps = db.execute(
        select(TenantCapabilities).where(TenantCapabilities.tenant_id == tenant.id)
    ).scalar_one_or_none()
    overrides = (caps.override_flags or {}) if caps is not None else {}

    values: dict[str, Any] = {}
    fields = set(plan_capability_fields())
    for feature in FEATURES.values():
        for column in (feature.flag, feature.limit, feature.grant):
            if column is not None:
                fields.add(column)
    for field in fields:
        on_plan = plan_values is not None and field in plan_values
        if caps is not None and (not on_plan or field in overrides):
            values[field] = getattr(caps, field, None)
        elif on_plan:
            values[field] = plan_values[field]
        else:
            values[field] = None
    # Unknown (no plan row, no capabilities row) stays off: booleans None, limits None would
    # read as unlimited, so close them explicitly.
    if caps is None and plan_values is None:
        for feature in FEATURES.values():
            if feature.limit is not None:
                values[feature.limit] = 0

    return Entitlements(
        db=db,
        tenant_id=tenant.id,
        tier=tier,
        subscription_status=str(tenant.subscription_status),
        values=values,
    )


def get_entitlements_for_tenant_id(db: Session, tenant_id: UUID) -> Entitlements | None:
    """`get_entitlements` for callers that only hold an id; None when the tenant is gone."""
    tenant = db.get(Tenant, tenant_id)
    return get_entitlements(tenant, db) if tenant is not None else None


def tenant_has(db: Session, tenant_id: UUID, key: str) -> bool:
    """Runtime-path check: True only when the tenant exists and its plan includes `key`."""
    entitlements = get_entitlements_for_tenant_id(db, tenant_id)
    return bool(entitlements and entitlements.enabled(key))


def require_feature(db: Session, tenant_id: UUID, key: str) -> Entitlements:
    """Config-save and admin-endpoint gate: raises FeatureNotInPlan unless the plan has `key`."""
    entitlements = get_entitlements_for_tenant_id(db, tenant_id)
    if entitlements is None:
        raise FeatureNotInPlan(key, None, "This feature is not available for your account.")
    entitlements.require(key)
    return entitlements


def min_tier_for(db: Session, key: str) -> str | None:
    """Lowest tier from which `key` stays included for every higher tier.

    None when the feature is a platform grant only (no plan column), e.g. voice.
    """
    feature = FEATURES[key]
    if feature.flag is None and feature.limit is None:
        return None

    def tier_includes(tier: str) -> bool:
        values = _tier_values(db, tier) or plan_capabilities(tier)
        if feature.flag is not None and not values.get(feature.flag):
            return False
        if feature.limit is not None:
            limit = values.get(feature.limit)
            if limit is not None and limit <= 0:
                return False
        return True

    result: str | None = None
    for tier in reversed(TIER_ORDER):
        if not tier_includes(tier):
            break
        result = tier
    return result


def _gate_message(key: str, min_tier: str | None) -> str:
    label = FEATURES[key].label
    if min_tier is None:
        return f"Your account does not include {label}."
    return f"Your plan does not include {label}. Upgrade to {_TIER_NAMES.get(min_tier, min_tier)} to use it."


def apply_plan_to_capabilities(
    capabilities: TenantCapabilities,
    plan_config: dict[str, Any],
    *,
    respect_overrides: bool,
) -> list[str]:
    """Copy a plan's capability values onto a tenant's stored capabilities.

    The single writer for tier syncs (upgrade, downgrade, webhook-confirmed payment, platform
    override, promotional grant and its expiry). With `respect_overrides` a field listed in
    `override_flags` keeps its manual value; without it every plan field is written and the
    flags of the fields this call overwrote are cleared, so a downgrade cannot keep a
    manually-raised limit. Returns the fields that were written.
    """
    existing_flags = capabilities.override_flags or {}
    protected = existing_flags if respect_overrides else {}

    written: list[str] = []
    cleared: list[str] = []
    for field in plan_capability_fields():
        if field not in plan_config or field in protected:
            continue
        setattr(capabilities, field, plan_config[field])
        written.append(field)
        if not respect_overrides and field in existing_flags:
            cleared.append(field)

    if cleared:
        # New dict: SQLAlchemy does not track in-place mutation of a JSON column.
        capabilities.override_flags = {k: v for k, v in existing_flags.items() if k not in cleared}
    return written


def new_tenant_capabilities(
    db: Session, tenant_id: UUID, tier: str = "trial"
) -> TenantCapabilities:
    """Capabilities row for a new tenant, copied from the tier's plan row.

    Falls back to the catalog values when the plans table has no row for the tier (fresh test
    databases), so a tenant never starts with an invented set of limits.
    """
    values = _tier_values(db, tier) or plan_capabilities(tier)
    return TenantCapabilities(
        tenant_id=tenant_id,
        can_create_tickets=True,
        can_upload_knowledge=True,
        # null() because the ORM turns a plain None into the column default (e.g. 0 WhatsApp
        # numbers) on insert; an unlimited plan value must reach the database as NULL.
        **{
            name: values[name] if values[name] is not None else null()
            for name in plan_capability_fields()
            if name in values
        },
    )
