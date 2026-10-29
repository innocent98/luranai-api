"""Shared builders for the entitlements tests (Postgres session with the seeded plans)."""

from uuid import uuid4

from app.core.security import create_access_token, get_password_hash
from app.db.models.tenant import Tenant, TenantAIConfig, TenantCapabilities
from app.db.models.user import User, UserRole
from app.services.entitlements import new_tenant_capabilities

# The tier matrix from the contract (docs/specs/2026-10-28-entitlements-contract.md), written out
# by hand so a wrong plan row cannot hide behind a test that reads the same catalog.
SALES_CLOSE = {
    "trial": True,
    "lite": False,
    "professional": True,
    "growth": True,
    "enterprise": True,
}
ANALYTICS = {"trial": False, "lite": True, "professional": True, "growth": True, "enterprise": True}
LOOKUP_AND_WEBHOOKS = {
    "trial": False,
    "lite": False,
    "professional": True,
    "growth": True,
    "enterprise": True,
}
AI_ACTIONS = {"trial": 2, "lite": 2, "professional": 15, "growth": 40, "enterprise": None}
WHATSAPP = {"trial": 1, "lite": 1, "professional": 2, "growth": 5, "enterprise": None}
TIERS = ("trial", "lite", "professional", "growth", "enterprise")


def make_tenant(
    db, tier: str = "trial", *, with_caps: bool = True, whatsapp_override: bool | None = None
):
    """A tenant on `tier`. WhatsApp follows the plan unless `whatsapp_override` is given: then the
    stored flag holds that value and `override_flags` records it, as a platform-admin edit does."""
    prefix = uuid4().hex[:8]
    tenant = Tenant(
        id=uuid4(),
        name=f"ent_{prefix}",
        display_name=f"Ent {prefix}",
        api_key_hash=uuid4().hex,
        subscription_tier=tier,
        subscription_status="active" if tier != "trial" else "trialing",
    )
    db.add(tenant)
    db.flush()
    db.add(
        TenantAIConfig(
            id=uuid4(),
            tenant_id=tenant.id,
            brand_voice={},
            escalation_keywords=[],
            max_response_tokens=250,
            grounding_requirement=True,
        )
    )
    if with_caps:
        caps = new_tenant_capabilities(db, tenant.id, tier)
        if whatsapp_override is not None:
            caps.can_use_whatsapp = whatsapp_override
            caps.override_flags = {"can_use_whatsapp": True}
        db.add(caps)
    db.commit()
    return tenant


def make_user(db, tenant, role: UserRole = UserRole.TENANT_ADMIN):
    prefix = uuid4().hex[:8]
    user = User(
        email=f"u_{prefix}@example.com",
        hashed_password=get_password_hash("testpassword123"),
        full_name=f"User {prefix}",
        role=role,
        tenant_id=tenant.id,
        is_active=True,
    )
    db.add(user)
    db.commit()
    token = create_access_token(
        subject=str(user.id),
        additional_claims={"role": user.role.value, "tenant_id": str(user.tenant_id)},
    )
    return user, {"Authorization": f"Bearer {token}"}


def caps_of(db, tenant) -> TenantCapabilities:
    db.expire_all()
    return db.query(TenantCapabilities).filter_by(tenant_id=tenant.id).one()
