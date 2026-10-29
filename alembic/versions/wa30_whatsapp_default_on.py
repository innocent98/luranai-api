"""WhatsApp becomes a plan capability (on for every tier) instead of a platform grant

Revision ID: wa30whatsappdefaulton
Revises: ref10referral
Create Date: 2026-10-29

Until now `can_use_whatsapp` lived only on tenant_capabilities (default false) and a platform admin
granted it one tenant at a time. The owner reversed that: every tenant gets WhatsApp by plan, with
the number of linked numbers still plan-driven (`max_whatsapp_numbers`, already on the plan rows
and already backfilled onto tenants by ent10entitlements, so it is not touched here).

* New: subscription_plans.can_use_whatsapp (Boolean NOT NULL, server default false), set TRUE for
  all five tiers.
* Plan feature strings: "WhatsApp (beta, on request): N number(s)" -> "WhatsApp (beta): N
  number(s)", replacing only that one element and only where the stored list holds exactly the old
  string. A list a platform admin edited by hand is left alone (a warning names the tier).
* Tenant backfill: tenant_capabilities.can_use_whatsapp = the plan value of the tenant's
  subscription_tier, ONLY where override_flags has no `can_use_whatsapp` key. A platform-admin edit
  (true or false) is recorded in override_flags and survives. NULL override_flags = no override.
* The global kill switch WHATSAPP_ENABLED is untouched: nothing here turns WhatsApp on in a
  deployment where the switch is off.

Idempotent (column add checks first, data steps converge: a second run changes 0 rows) and
reversible (downgrade restores the old feature strings, sets the stored flag back to FALSE for
tenants without the override key, then drops the plan column; the tenant_capabilities column
existed before and stays).
"""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "wa30whatsappdefaulton"
down_revision: str | Sequence[str] | None = "ref10referral"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FLAG = "can_use_whatsapp"

# Frozen point-in-time values (do not import app code into a migration).
TIERS = ("trial", "lite", "professional", "growth", "enterprise")

# tier: (feature string before this migration, feature string after it)
WHATSAPP_FEATURE = {
    "trial": (
        "WhatsApp (beta, on request): 1 number",
        "WhatsApp (beta): 1 number",
    ),
    "lite": (
        "WhatsApp (beta, on request): 1 number",
        "WhatsApp (beta): 1 number",
    ),
    "professional": (
        "WhatsApp (beta, on request): 2 numbers",
        "WhatsApp (beta): 2 numbers",
    ),
    "growth": (
        "WhatsApp (beta, on request): 5 numbers",
        "WhatsApp (beta): 5 numbers",
    ),
    "enterprise": (
        "WhatsApp (beta, on request): unlimited numbers",
        "WhatsApp (beta): unlimited numbers",
    ),
}


def _columns(bind, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns(table)}


def _json_list(raw) -> list:
    if raw is None:
        return []
    return json.loads(raw) if isinstance(raw, str) else list(raw)


def _json_dict(raw) -> dict:
    if raw is None:
        return {}
    return json.loads(raw) if isinstance(raw, str) else dict(raw)


def set_plan_flag(bind) -> None:
    """Every tier includes WhatsApp."""
    for tier in TIERS:
        bind.execute(
            sa.text(f"UPDATE subscription_plans SET {FLAG} = TRUE WHERE tier = :tier"),
            {"tier": tier},
        )


def rewrite_plan_features(bind, reverse: bool = False) -> None:
    """Swap the WhatsApp feature string of each tier (the one element only), or swap it back."""
    for tier, (old, new) in WHATSAPP_FEATURE.items():
        source, target = (new, old) if reverse else (old, new)
        raw = bind.execute(
            sa.text("SELECT features FROM subscription_plans WHERE tier = :tier"), {"tier": tier}
        ).scalar()
        if raw is None:
            continue
        current = _json_list(raw)
        if source in current:
            updated = [target if item == source else item for item in current]
        elif target in current:
            continue  # already converged
        else:
            print(f"WARNING: plan {tier!r} features were edited by hand; WhatsApp line unchanged")
            continue
        bind.execute(
            sa.text("UPDATE subscription_plans SET features = CAST(:f AS JSON) WHERE tier = :tier"),
            {"f": json.dumps(updated), "tier": tier},
        )


def backfill_tenants(bind) -> int:
    """Copy the plan flag onto every tenant without an override. Returns rows changed."""
    plans = {
        row.tier: getattr(row, FLAG)
        for row in bind.execute(sa.text(f"SELECT tier, {FLAG} FROM subscription_plans"))
    }
    rows = bind.execute(
        sa.text(
            f"SELECT c.id, t.subscription_tier AS tier, c.override_flags, c.{FLAG} AS stored "
            "FROM tenant_capabilities c JOIN tenants t ON t.id = c.tenant_id"
        )
    ).fetchall()

    changed = 0
    for row in rows:
        if row.tier not in plans:
            continue
        # A key in override_flags means a platform admin set this by hand (true or false): keep it.
        # override_flags is a plain JSON column and may be NULL.
        if FLAG in _json_dict(row.override_flags):
            continue
        wanted = bool(plans[row.tier])
        if row.stored == wanted:
            continue
        bind.execute(
            sa.text(f"UPDATE tenant_capabilities SET {FLAG} = :v WHERE id = :id"),
            {"v": wanted, "id": row.id},
        )
        changed += 1
    return changed


def unwind_tenants(bind) -> int:
    """Back to the grant model: off for everyone who was not set by hand. Returns rows changed."""
    rows = bind.execute(
        sa.text(f"SELECT id, override_flags, {FLAG} AS stored FROM tenant_capabilities")
    ).fetchall()
    changed = 0
    for row in rows:
        if FLAG in _json_dict(row.override_flags) or not row.stored:
            continue
        bind.execute(
            sa.text(f"UPDATE tenant_capabilities SET {FLAG} = FALSE WHERE id = :id"),
            {"id": row.id},
        )
        changed += 1
    return changed


def upgrade() -> None:
    bind = op.get_bind()
    # Both tables are read on the chat path: fail fast on the lock, don't queue behind it.
    op.execute("SET LOCAL lock_timeout = '5s'")

    if FLAG not in _columns(bind, "subscription_plans"):
        op.add_column(
            "subscription_plans",
            sa.Column(FLAG, sa.Boolean(), nullable=False, server_default=sa.false()),
        )
    set_plan_flag(bind)
    rewrite_plan_features(bind)
    backfill_tenants(bind)


def downgrade() -> None:
    bind = op.get_bind()
    op.execute("SET LOCAL lock_timeout = '5s'")

    unwind_tenants(bind)
    rewrite_plan_features(bind, reverse=True)
    if FLAG in _columns(bind, "subscription_plans"):
        op.drop_column("subscription_plans", FLAG)
