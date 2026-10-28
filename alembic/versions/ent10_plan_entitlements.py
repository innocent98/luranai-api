"""plan entitlements: sales/CRM/recording/ratings gates, plan-driven WhatsApp numbers, Lite price

Revision ID: ent10entitlements
Revises: act10onboarding
Create Date: 2026-10-28

Pricing and entitlements (docs/superpowers/specs/2026-10-27-pricing-and-entitlements-design.md,
contract 2026-10-28). Adds the gate columns to BOTH subscription_plans and tenant_capabilities,
writes the approved tier matrix into the plan rows, and backfills every existing tenant from its
tier.

* New on subscription_plans and tenant_capabilities: can_use_sales_close,
  can_use_crm_notifications, can_export_ratings (all default false). subscription_plans also
  gains can_use_screen_recording and max_whatsapp_numbers (tenant_capabilities already had them).
* tenant_capabilities.can_use_screen_recording: server default true -> false (plan-driven now).
* max_whatsapp_numbers becomes nullable on both tables (NULL = unlimited, Enterprise), and
  tenant_capabilities.max_knowledge_mb too (the Enterprise plan's unlimited storage could not be
  synced onto the NOT NULL column).
* Plan data: Lite price 900000 -> 1500000 kobo (annual 9000000 -> 15000000), Lite AI actions
  5 -> 2, the gate flags and WhatsApp numbers per tier, and regenerated features strings. A plan
  whose strings a platform admin edited by hand is left alone (a warning names it).
* Tenant backfill: only the synced fields below, only where the field is not in override_flags.
  max_whatsapp_numbers is raised from 0/NULL but never lowered, so a hand-granted number
  survives. The single Growth override tenant ends up with every gate on because the Growth plan
  has them on.

Idempotent (column adds check first, data steps converge) and reversible (downgrade drops the
columns, restores the old price, Lite actions, screen-recording default and feature strings).
The seed (scripts/seed_data.py) and this migration write the same numbers: the plan values live
once in app/services/plan_catalog.py and are frozen here as a point-in-time copy.
"""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "ent10entitlements"
down_revision: str | Sequence[str] | None = "act10onboarding"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_BOOL_COLUMNS = ("can_use_sales_close", "can_use_crm_notifications", "can_export_ratings")

# Frozen point-in-time values (do not import app code into a migration).
# tier: (sales_close, crm_notifications, screen_recording, ratings_export, whatsapp_numbers)
GATES = {
    "trial": (True, True, True, True, 1),
    "lite": (False, False, False, False, 1),
    "professional": (True, True, True, True, 2),
    "growth": (True, True, True, True, 5),
    "enterprise": (True, True, True, True, None),
}
LITE_PRICE = {"new": (1_500_000, 15_000_000), "old": (900_000, 9_000_000)}
LITE_ACTIONS = {"new": 2, "old": 5}

# Plan feature strings: before (scripts/seed_data.py at act10onboarding) and after.
OLD_FEATURES = {
    "trial": [
        "2 knowledge sources",
        "10MB storage",
        "200 conversations/month",
        "1 team member",
        "1 flow",
        "2 automation actions",
        "100 automation executions/month",
        "AI chat automation",
        "AI support widget",
        "Email support",
    ],
    "lite": [
        "20 knowledge sources",
        "50MB storage",
        "2,000 conversations/month",
        "3 team members",
        "3 flows",
        "5 automation actions",
        "500 automation executions/month",
        "AI chat automation",
        "URL scraping",
        "Analytics dashboard",
        "Full support",
    ],
    "professional": [
        "50 knowledge sources",
        "200MB storage",
        "10,000 conversations/month",
        "10 team members",
        "10 flows",
        "15 automation actions",
        "5,000 automation executions/month",
        "AI chat automation",
        "Lookup chains",
        "Webhook integrations",
        "URL scraping",
        "Analytics dashboard",
        "Full support",
    ],
    "growth": [
        "100 knowledge sources",
        "500MB storage",
        "25,000 conversations/month",
        "20 team members",
        "25 flows",
        "40 automation actions",
        "25,000 automation executions/month",
        "AI chat automation",
        "Lookup chains",
        "Up to 10 webhooks per flow",
        "URL scraping",
        "Analytics dashboard",
        "Full support",
    ],
    "enterprise": [
        "Unlimited knowledge sources",
        "Unlimited storage",
        "Volume-based pricing (25K-300K+)",
        "Unlimited team members",
        "Unlimited flows",
        "Unlimited automation actions",
        "Custom automation executions/month",
        "AI chat automation",
        "Lookup chains",
        "Custom webhook limits",
        "Dedicated account manager",
        "Custom integrations",
        "SLA guarantees",
        "24/7 premium support",
    ],
}

NEW_FEATURES = {
    "trial": [
        "2 knowledge sources",
        "10MB storage",
        "200 conversations/month",
        "1 team member",
        "1 flow",
        "2 automation actions",
        "100 automation executions/month",
        "AI chat automation",
        "Meeting booking and quotes",
        "Slack and CRM notifications",
        "Screen recording",
        "Ratings breakdown and CSV export",
        "WhatsApp (beta, on request): 1 number",
        "AI support widget",
        "Email support",
    ],
    "lite": [
        "20 knowledge sources",
        "50MB storage",
        "2,000 conversations/month",
        "3 team members",
        "3 flows",
        "2 automation actions",
        "500 automation executions/month",
        "AI chat automation",
        "URL scraping",
        "Analytics dashboard",
        "WhatsApp (beta, on request): 1 number",
        "Full support",
    ],
    "professional": [
        "50 knowledge sources",
        "200MB storage",
        "10,000 conversations/month",
        "10 team members",
        "10 flows",
        "15 automation actions",
        "5,000 automation executions/month",
        "AI chat automation",
        "Lookup chains",
        "Up to 3 webhooks per flow",
        "URL scraping",
        "Analytics dashboard",
        "Meeting booking and quotes",
        "Slack and CRM notifications",
        "Screen recording",
        "Ratings breakdown and CSV export",
        "WhatsApp (beta, on request): 2 numbers",
        "Full support",
    ],
    "growth": [
        "100 knowledge sources",
        "500MB storage",
        "25,000 conversations/month",
        "20 team members",
        "25 flows",
        "40 automation actions",
        "25,000 automation executions/month",
        "AI chat automation",
        "Lookup chains",
        "Up to 10 webhooks per flow",
        "URL scraping",
        "Analytics dashboard",
        "Meeting booking and quotes",
        "Slack and CRM notifications",
        "Screen recording",
        "Ratings breakdown and CSV export",
        "WhatsApp (beta, on request): 5 numbers",
        "Full support",
    ],
    "enterprise": [
        "Unlimited knowledge sources",
        "Unlimited storage",
        "Unlimited conversations/month",
        "Unlimited team members",
        "Unlimited flows",
        "Unlimited automation actions",
        "Unlimited automation executions/month",
        "AI chat automation",
        "Lookup chains",
        "Unlimited webhooks per flow",
        "URL scraping",
        "Analytics dashboard",
        "Meeting booking and quotes",
        "Slack and CRM notifications",
        "Screen recording",
        "Ratings breakdown and CSV export",
        "WhatsApp (beta, on request): unlimited numbers",
        "Volume-based pricing (25K-300K+)",
        "Dedicated account manager",
        "Custom integrations",
        "SLA guarantees",
        "24/7 premium support",
    ],
}


def _columns(bind, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns(table)}


def _add_column_if_missing(bind, table: str, column: sa.Column) -> None:
    if column.name not in _columns(bind, table):
        op.add_column(table, column)


def _json_list(raw) -> list:
    if raw is None:
        return []
    return json.loads(raw) if isinstance(raw, str) else list(raw)


def _json_dict(raw) -> dict:
    if raw is None:
        return {}
    return json.loads(raw) if isinstance(raw, str) else dict(raw)


def apply_plan_data(bind, reverse: bool = False) -> None:
    """Write (or, reversed, undo) the plan rows: gates, WhatsApp numbers, Lite price/actions."""
    if not reverse:  # on the way down the columns are dropped instead
        for tier, (sales, crm, recording, ratings, whatsapp) in GATES.items():
            bind.execute(
                sa.text(
                    "UPDATE subscription_plans SET can_use_sales_close = :s, "
                    "can_use_crm_notifications = :c, can_use_screen_recording = :r, "
                    "can_export_ratings = :x, max_whatsapp_numbers = :w WHERE tier = :tier"
                ),
                {"s": sales, "c": crm, "r": recording, "x": ratings, "w": whatsapp, "tier": tier},
            )

    which = "old" if reverse else "new"
    monthly, annual = LITE_PRICE[which]
    bind.execute(
        sa.text(
            "UPDATE subscription_plans SET monthly_price_kobo = :m, annual_price_kobo = :a, "
            "max_automation_actions = :act WHERE tier = 'lite'"
        ),
        {"m": monthly, "a": annual, "act": LITE_ACTIONS[which]},
    )

    source, target = (NEW_FEATURES, OLD_FEATURES) if reverse else (OLD_FEATURES, NEW_FEATURES)
    for tier, new_strings in target.items():
        raw = bind.execute(
            sa.text("SELECT features FROM subscription_plans WHERE tier = :tier"), {"tier": tier}
        ).scalar()
        if raw is None:
            continue
        current = _json_list(raw)
        if current == new_strings:
            continue
        if current != source[tier]:
            print(f"WARNING: plan {tier!r} features were edited by hand; left unchanged")
            continue
        bind.execute(
            sa.text("UPDATE subscription_plans SET features = CAST(:f AS JSON) WHERE tier = :tier"),
            {"f": json.dumps(new_strings), "tier": tier},
        )


def backfill_tenants(bind) -> int:
    """Sync the new plan-driven fields of every tenant from its tier. Returns rows changed."""
    plans = {
        row.tier: row
        for row in bind.execute(
            sa.text(
                "SELECT tier, can_use_sales_close, can_use_crm_notifications, "
                "can_use_screen_recording, can_export_ratings, max_whatsapp_numbers, "
                "max_automation_actions FROM subscription_plans"
            )
        )
    }
    rows = bind.execute(
        sa.text(
            "SELECT c.id, t.subscription_tier AS tier, c.override_flags, "
            "c.can_use_sales_close, c.can_use_crm_notifications, c.can_use_screen_recording, "
            "c.can_export_ratings, c.max_whatsapp_numbers, c.max_automation_actions "
            "FROM tenant_capabilities c JOIN tenants t ON t.id = c.tenant_id"
        )
    ).fetchall()

    changed = 0
    for row in rows:
        plan = plans.get(row.tier)
        if plan is None:
            continue
        overrides = _json_dict(row.override_flags)
        updates: dict = {}
        for field in (
            "can_use_sales_close",
            "can_use_crm_notifications",
            "can_use_screen_recording",
            "can_export_ratings",
        ):
            if field not in overrides and getattr(row, field) != getattr(plan, field):
                updates[field] = getattr(plan, field)
        # Only Lite's AI-action limit changed (5 -> 2); other tiers' limits are not ours to touch.
        if (
            row.tier == "lite"
            and "max_automation_actions" not in overrides
            and row.max_automation_actions != plan.max_automation_actions
        ):
            updates["max_automation_actions"] = plan.max_automation_actions
        # Raise from 0/NULL, never lower a number somebody granted by hand.
        if "max_whatsapp_numbers" not in overrides and row.max_whatsapp_numbers in (0, None):
            if plan.max_whatsapp_numbers != row.max_whatsapp_numbers:
                updates["max_whatsapp_numbers"] = plan.max_whatsapp_numbers
        if not updates:
            continue
        assignments = ", ".join(f"{name} = :{name}" for name in updates)
        bind.execute(
            sa.text(f"UPDATE tenant_capabilities SET {assignments} WHERE id = :id"),
            {**updates, "id": row.id},
        )
        changed += 1
    return changed


def unwind_tenants(bind) -> None:
    """Undo the backfill where it can be undone: screen recording was on for everyone."""
    rows = bind.execute(
        sa.text(
            "SELECT c.id, t.subscription_tier AS tier, c.override_flags, "
            "c.can_use_screen_recording, c.max_automation_actions "
            "FROM tenant_capabilities c JOIN tenants t ON t.id = c.tenant_id"
        )
    ).fetchall()
    for row in rows:
        overrides = _json_dict(row.override_flags)
        updates: dict = {}
        if "can_use_screen_recording" not in overrides and not row.can_use_screen_recording:
            updates["can_use_screen_recording"] = True
        if (
            row.tier == "lite"
            and "max_automation_actions" not in overrides
            and row.max_automation_actions == LITE_ACTIONS["new"]
        ):
            updates["max_automation_actions"] = LITE_ACTIONS["old"]
        if updates:
            assignments = ", ".join(f"{name} = :{name}" for name in updates)
            bind.execute(
                sa.text(f"UPDATE tenant_capabilities SET {assignments} WHERE id = :id"),
                {**updates, "id": row.id},
            )


def upgrade() -> None:
    bind = op.get_bind()
    # Both tables are read on the chat path: fail fast on the lock, don't queue behind it.
    op.execute("SET LOCAL lock_timeout = '5s'")

    for table in ("subscription_plans", "tenant_capabilities"):
        for name in NEW_BOOL_COLUMNS:
            _add_column_if_missing(
                bind,
                table,
                sa.Column(name, sa.Boolean(), nullable=False, server_default=sa.false()),
            )
    _add_column_if_missing(
        bind,
        "subscription_plans",
        sa.Column(
            "can_use_screen_recording", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    _add_column_if_missing(
        bind,
        "subscription_plans",
        # 0 for any plan row the data step does not name; dropped again below so a plain None
        # means unlimited (NULL) when the ORM inserts it.
        sa.Column("max_whatsapp_numbers", sa.Integer(), nullable=True, server_default="0"),
    )
    op.alter_column(
        "tenant_capabilities",
        "can_use_screen_recording",
        server_default=sa.false(),
        existing_type=sa.Boolean(),
        existing_nullable=False,
    )
    op.alter_column(
        "tenant_capabilities",
        "max_whatsapp_numbers",
        nullable=True,
        existing_type=sa.Integer(),
        existing_server_default="0",
    )
    # The Enterprise plan has unlimited storage (NULL); syncing it onto a NOT NULL column failed.
    op.alter_column(
        "tenant_capabilities", "max_knowledge_mb", nullable=True, existing_type=sa.Integer()
    )

    apply_plan_data(bind)
    op.alter_column(
        "subscription_plans",
        "max_whatsapp_numbers",
        server_default=None,
        existing_type=sa.Integer(),
    )
    backfill_tenants(bind)


def downgrade() -> None:
    bind = op.get_bind()
    op.execute("SET LOCAL lock_timeout = '5s'")

    apply_plan_data(bind, reverse=True)
    unwind_tenants(bind)

    # Without the grant the number meant nothing; before this migration it was 0 by default.
    op.execute(
        "UPDATE tenant_capabilities SET max_whatsapp_numbers = 0 WHERE can_use_whatsapp = FALSE"
    )
    # NULL (unlimited) cannot survive a NOT NULL column: store a limit nobody reaches instead.
    op.execute(
        "UPDATE tenant_capabilities SET max_knowledge_mb = 1000000 WHERE max_knowledge_mb IS NULL"
    )
    op.alter_column(
        "tenant_capabilities", "max_knowledge_mb", nullable=False, existing_type=sa.Integer()
    )
    # NULL (unlimited WhatsApp numbers) cannot survive a NOT NULL column.
    op.execute(
        "UPDATE tenant_capabilities SET max_whatsapp_numbers = 0 WHERE max_whatsapp_numbers IS NULL"
    )
    op.alter_column(
        "tenant_capabilities",
        "max_whatsapp_numbers",
        nullable=False,
        existing_type=sa.Integer(),
        existing_server_default="0",
    )
    op.alter_column(
        "tenant_capabilities",
        "can_use_screen_recording",
        server_default=sa.true(),
        existing_type=sa.Boolean(),
        existing_nullable=False,
    )

    for name in NEW_BOOL_COLUMNS:
        for table in ("tenant_capabilities", "subscription_plans"):
            if name in _columns(bind, table):
                op.drop_column(table, name)
    for name in ("can_use_screen_recording", "max_whatsapp_numbers"):
        if name in _columns(bind, "subscription_plans"):
            op.drop_column("subscription_plans", name)
