"""New monthly conversation limits: Trial 100, Lite 1,000, Professional 5,000, Growth 10,000

Revision ID: cl10conversationlimits
Revises: wa30whatsappdefaulton
Create Date: 2026-10-29

The owner approved lower monthly conversation limits (2026-10-29). The cost per conversation is
small (about 7,050 tokens, under 6 naira in the worst case), so the new numbers are a packaging
decision, not a cost one. Enterprise stays unlimited (NULL) and is not touched.

    tier          old      new
    trial         200      100
    lite          2,000    1,000
    professional  10,000   5,000
    growth        25,000   10,000

* subscription_plans.max_conversations_per_month: set to the new number, only where the stored
  value is exactly the old one (a plan row a platform admin changed by hand is left alone and a
  warning names it).
* subscription_plans.features: the one "N conversations/month" element is swapped, only where
  the stored list holds exactly the old string (same rule: a hand-edited list is left alone). No
  other element is read or written, so the WhatsApp line wa30whatsappdefaulton rewrote is safe.
* tenant_capabilities.max_conversations_per_month: set to the new number for tenants of that tier
  whose stored value is exactly the old plan value and whose override_flags has no
  `max_conversations_per_month` key. A platform-admin override (a tenant kept at a higher or lower
  number by hand) survives, and so does a value that is neither the old nor the new plan number.
  (ent10entitlements' backfill did not select this column; this one does.)
* Nothing is deleted and no conversation is touched. A tenant already above its new limit this
  month keeps every existing conversation and can still continue them; only opening a NEW counted
  conversation is refused (402 conversation_limit_reached) until the month resets. The migration
  prints a WARNING naming each such tenant so the owner can set an override first or tell them.

Idempotent (a second run changes 0 rows) and reversible (downgrade restores the old numbers under
the mirror-image rules: only where the stored value is exactly the new one and not overridden).
The plan values live once in app/services/plan_catalog.py and are frozen here as a point-in-time
copy (no app imports in a migration).
"""

import json
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "cl10conversationlimits"
down_revision: str | Sequence[str] | None = "wa30whatsappdefaulton"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FIELD = "max_conversations_per_month"

# Frozen point-in-time values: tier -> (old, new). Enterprise (unlimited, NULL) is not listed.
LIMITS: dict[str, tuple[int, int]] = {
    "trial": (200, 100),
    "lite": (2_000, 1_000),
    "professional": (10_000, 5_000),
    "growth": (25_000, 10_000),
}


def _feature(limit: int) -> str:
    return f"{limit:,} conversations/month"


def _json_list(raw) -> list:
    if raw is None:
        return []
    return json.loads(raw) if isinstance(raw, str) else list(raw)


# True when override_flags is a JSON object holding the field's key (a platform admin set it by
# hand). NULL, JSON null, lists, strings and numbers are "no override". jsonb_exists is the
# function form of the jsonb `?` operator, which would clash with bind-parameter syntax.
_HAS_OVERRIDE = (
    "COALESCE(jsonb_typeof(CAST(c.override_flags AS jsonb)) = 'object' "
    f"AND jsonb_exists(CAST(c.override_flags AS jsonb), '{FIELD}'), FALSE)"
)


def rewrite_plan_limits(bind, reverse: bool = False) -> None:
    """Move each plan row's limit and its one feature string old -> new (or back)."""
    for tier, pair in LIMITS.items():
        source, target = (pair[1], pair[0]) if reverse else pair

        stored = bind.execute(
            sa.text(f"SELECT {FIELD} FROM subscription_plans WHERE tier = :tier"), {"tier": tier}
        ).fetchone()
        if stored is not None:
            if stored[0] == source:
                bind.execute(
                    sa.text(f"UPDATE subscription_plans SET {FIELD} = :v WHERE tier = :tier"),
                    {"v": target, "tier": tier},
                )
            elif stored[0] != target:
                print(
                    f"WARNING: plan {tier!r} {FIELD} is {stored[0]}, not {source}; left unchanged"
                )

        raw = bind.execute(
            sa.text("SELECT features FROM subscription_plans WHERE tier = :tier"), {"tier": tier}
        ).scalar()
        if raw is None:
            continue
        current = _json_list(raw)
        if _feature(source) in current:
            updated = [_feature(target) if item == _feature(source) else item for item in current]
        elif _feature(target) in current:
            continue  # already converged
        else:
            print(f"WARNING: plan {tier!r} features were edited by hand; conversations unchanged")
            continue
        bind.execute(
            sa.text("UPDATE subscription_plans SET features = CAST(:f AS JSON) WHERE tier = :tier"),
            {"f": json.dumps(updated), "tier": tier},
        )


def rewrite_tenants(bind, reverse: bool = False) -> dict[str, int]:
    """Move tenants of each tier from the old plan number to the new one (or back).

    Only rows holding exactly the source number and no override key. Returns rows changed by tier.
    """
    changed: dict[str, int] = {}
    for tier, pair in LIMITS.items():
        source, target = (pair[1], pair[0]) if reverse else pair
        result = bind.execute(
            sa.text(
                f"UPDATE tenant_capabilities c SET {FIELD} = :target "
                "FROM tenants t JOIN subscription_plans p ON p.tier = t.subscription_tier "
                "WHERE t.id = c.tenant_id AND p.tier = :tier "
                f"AND c.{FIELD} = :source AND NOT {_HAS_OVERRIDE}"
            ),
            {"target": target, "source": source, "tier": tier},
        )
        changed[tier] = result.rowcount
    return changed


def warn_over_limit(bind) -> int:
    """Print one WARNING per tenant whose usage this month already exceeds its new limit."""
    # conversation_sessions.created_at is timestamptz: pass an aware UTC instant, not a naive one.
    start = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    flagged = 0
    for tier, (_, new) in LIMITS.items():
        rows = bind.execute(
            sa.text(
                "SELECT s.tenant_id, count(*) AS used "
                "FROM conversation_sessions s "
                "JOIN tenants t ON t.id = s.tenant_id "
                "JOIN tenant_capabilities c ON c.tenant_id = t.id "
                "JOIN subscription_plans p ON p.tier = t.subscription_tier "
                f"WHERE p.tier = :tier AND c.{FIELD} = :new AND s.created_at >= :start "
                "GROUP BY s.tenant_id HAVING count(*) > :new"
            ),
            {"tier": tier, "new": new, "start": start},
        ).fetchall()
        for row in rows:
            flagged += 1
            print(
                f"WARNING: tenant {row.tenant_id} ({tier}) has {row.used} conversations this "
                f"month, above the new limit of {new}; new conversations are refused until the "
                "month resets. Existing conversations are untouched."
            )
    return flagged


def upgrade() -> None:
    bind = op.get_bind()
    # Both tables are read on the chat path: fail fast on the lock, don't queue behind it.
    op.execute("SET LOCAL lock_timeout = '5s'")

    rewrite_plan_limits(bind)
    rewrite_tenants(bind)
    warn_over_limit(bind)


def downgrade() -> None:
    bind = op.get_bind()
    op.execute("SET LOCAL lock_timeout = '5s'")

    rewrite_tenants(bind, reverse=True)
    rewrite_plan_limits(bind, reverse=True)
