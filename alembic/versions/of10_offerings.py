"""Offerings catalog: six tables, two plan columns, the OFFERING_REQUEST notification event

Revision ID: of10offerings
Revises: wa40contactexclusion
Create Date: 2026-10-30

Docs: docs/superpowers/specs/2026-10-30-offerings-catalog-design.md (sections 6 and 12).

* New tables (all tenant-scoped, tenant FK ON DELETE CASCADE): `offerings`, `offering_images`,
  `offering_activity_days`, `tenant_offerings_settings`, `lead_offering_interests` (FK to leads
  CASCADE, to offerings SET NULL, so a snapshot survives an item being deleted) and
  `offering_misses`. Enum-typed columns are VARCHAR guarded by named CHECK constraints, the price
  must match its mode, and `(tenant_id, seq)` / `(tenant_id, external_ref)` are unique.
* New plan columns on BOTH subscription_plans and tenant_capabilities: `max_offerings` (NULL =
  unlimited) and `max_offering_images` (photos per item, NOT NULL default 4). Plan values:
  Trial 25/4, Lite 100/4, Professional 500/8, Growth 2,000/8, Enterprise unlimited/8.
* Plan feature strings gain one element ("Offerings: up to N items") right after the WhatsApp line
  (appended when a hand-edited list has no WhatsApp line); exact-match insert, never duplicated.
* Tenant backfill: each tenant's two numbers = the plan value of its tier, per field only where
  override_flags has no key for that field. A tenant whose tier has no plan row is not touched.
* `notif_event` gains `offering_request` (Postgres 15 allows ADD VALUE inside a transaction as
  long as the value is not used in it). Postgres cannot drop an enum value, so the downgrade
  leaves it in place; an unused extra label is harmless.

Idempotent (every create checks first; data steps converge, a second run changes 0 rows) and
reversible (downgrade drops the tables and both plan columns and removes the feature element).
The plan numbers live once in app/services/plan_catalog.py and are frozen here (no app imports).
"""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "of10offerings"
down_revision: str | Sequence[str] | None = "wa40contactexclusion"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MAX_PRICE = 10_000_000_000

# Frozen point-in-time values. tier: (max_offerings, max_offering_images)
LIMITS: dict[str, tuple[int | None, int]] = {
    "trial": (25, 4),
    "lite": (100, 4),
    "professional": (500, 8),
    "growth": (2000, 8),
    "enterprise": (None, 8),
}
OFFERINGS_FEATURE = {
    "trial": "Offerings: up to 25 items",
    "lite": "Offerings: up to 100 items",
    "professional": "Offerings: up to 500 items",
    "growth": "Offerings: up to 2,000 items",
    "enterprise": "Offerings: unlimited items",
}
PLAN_FIELDS = ("max_offerings", "max_offering_images")
TABLES_DROP_ORDER = (
    "lead_offering_interests",
    "offering_images",
    "offering_activity_days",
    "offering_misses",
    "tenant_offerings_settings",
    "offerings",
)


def _columns(bind, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns(table)}


def _json_list(raw) -> list:
    if raw is None:
        return []
    return json.loads(raw) if isinstance(raw, str) else list(raw)


def _check_in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _ts(name: str, *, nullable: bool = False, default: bool = False) -> sa.Column:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        server_default=sa.func.now() if default else None,
        nullable=nullable,
    )


# --- plan columns, values, feature strings, tenant backfill ---------------------------------


def add_plan_columns(bind) -> None:
    if "max_offerings" not in _columns(bind, "subscription_plans"):
        op.add_column("subscription_plans", sa.Column("max_offerings", sa.Integer(), nullable=True))
    if "max_offering_images" not in _columns(bind, "subscription_plans"):
        op.add_column(
            "subscription_plans",
            sa.Column("max_offering_images", sa.Integer(), nullable=False, server_default="4"),
        )
    if "max_offerings" not in _columns(bind, "tenant_capabilities"):
        op.add_column(
            "tenant_capabilities", sa.Column("max_offerings", sa.Integer(), nullable=True)
        )
    if "max_offering_images" not in _columns(bind, "tenant_capabilities"):
        op.add_column(
            "tenant_capabilities",
            sa.Column("max_offering_images", sa.Integer(), nullable=False, server_default="4"),
        )


def set_plan_values(bind) -> None:
    """Write each tier's numbers onto its plan row (convergent: same result on a second run)."""
    for tier, (items, photos) in LIMITS.items():
        bind.execute(
            sa.text(
                "UPDATE subscription_plans SET max_offerings = :items, "
                "max_offering_images = :photos WHERE tier = :tier"
            ),
            {"items": items, "photos": photos, "tier": tier},
        )


def rewrite_plan_features(bind, reverse: bool = False) -> None:
    """Insert (or, reversed, remove) the one Offerings element of each tier's feature list."""
    for tier, line in OFFERINGS_FEATURE.items():
        raw = bind.execute(
            sa.text("SELECT features FROM subscription_plans WHERE tier = :tier"), {"tier": tier}
        ).scalar()
        if raw is None:
            continue
        current = _json_list(raw)
        if reverse:
            if line not in current:
                continue
            updated = [item for item in current if item != line]
        else:
            if line in current:
                continue  # already converged
            position = next(
                (i + 1 for i, item in enumerate(current) if str(item).startswith("WhatsApp (beta")),
                len(current),
            )
            updated = [*current[:position], line, *current[position:]]
        bind.execute(
            sa.text("UPDATE subscription_plans SET features = CAST(:f AS JSON) WHERE tier = :tier"),
            {"f": json.dumps(updated), "tier": tier},
        )


def _has_override(field: str) -> str:
    # True when override_flags is a JSON object holding the field's key (a platform admin set it
    # by hand). NULL, JSON null, lists, strings and numbers are "no override". jsonb_exists is the
    # function form of the jsonb `?` operator, which would clash with bind-parameter syntax.
    return (
        "COALESCE(jsonb_typeof(CAST(c.override_flags AS jsonb)) = 'object' "
        f"AND jsonb_exists(CAST(c.override_flags AS jsonb), '{field}'), FALSE)"
    )


def backfill_tenants(bind) -> int:
    """Copy each plan number onto tenants whose field is not overridden. Returns rows changed.

    One set-based UPDATE per field; a tenant whose tier has no plan row is not joined.
    """
    changed = 0
    for field in PLAN_FIELDS:
        result = bind.execute(
            sa.text(
                f"UPDATE tenant_capabilities c SET {field} = p.{field} "
                "FROM tenants t JOIN subscription_plans p ON p.tier = t.subscription_tier "
                "WHERE t.id = c.tenant_id "
                f"AND c.{field} IS DISTINCT FROM p.{field} "
                f"AND NOT {_has_override(field)}"
            )
        )
        changed += result.rowcount
    return changed


# --- tables ---------------------------------------------------------------------------------


def _create_offerings() -> None:
    op.create_table(
        "offerings",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("category", sa.String(60), nullable=True),
        sa.Column("keywords", sa.String(200), nullable=True),
        sa.Column("price_mode", sa.String(20), nullable=False),
        sa.Column("price", sa.BigInteger(), nullable=True),
        sa.Column("price_note", sa.String(60), nullable=True),
        sa.Column("availability", sa.String(20), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=True),
        sa.Column("negotiable", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("external_ref", sa.String(80), nullable=True),
        _ts("availability_changed_at", nullable=True),
        _ts("archived_at", nullable=True),
        _ts("created_at", default=True),
        _ts("updated_at", default=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("tenant_id", "seq", name="uq_offerings_tenant_seq"),
        sa.UniqueConstraint("tenant_id", "external_ref", name="uq_offerings_tenant_external_ref"),
        sa.CheckConstraint(_check_in("kind", ("product", "service")), name="ck_offerings_kind"),
        sa.CheckConstraint(
            _check_in("price_mode", ("fixed", "from", "on_request")), name="ck_offerings_price_mode"
        ),
        sa.CheckConstraint(
            _check_in("availability", ("in_stock", "limited", "out_of_stock", "on_request")),
            name="ck_offerings_availability",
        ),
        sa.CheckConstraint(_check_in("status", ("active", "archived")), name="ck_offerings_status"),
        sa.CheckConstraint(
            "(price_mode = 'on_request' AND price IS NULL) OR "
            f"(price_mode <> 'on_request' AND price IS NOT NULL AND price > 0 "
            f"AND price <= {MAX_PRICE})",
            name="ck_offerings_price_matches_mode",
        ),
        sa.CheckConstraint("quantity IS NULL OR quantity >= 0", name="ck_offerings_quantity"),
    )
    op.create_index("ix_offerings_id", "offerings", ["id"])
    op.create_index("ix_offerings_tenant_id", "offerings", ["tenant_id"])
    op.create_index("ix_offerings_tenant_status", "offerings", ["tenant_id", "status"])


def _create_offering_images() -> None:
    op.create_table(
        "offering_images",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("offering_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("cloudinary_public_id", sa.String(255), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("bytes", sa.Integer(), nullable=True),
        _ts("created_at", default=True),
        sa.ForeignKeyConstraint(["offering_id"], ["offerings.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("offering_id", "position", name="uq_offering_images_position"),
    )
    op.create_index("ix_offering_images_id", "offering_images", ["id"])
    op.create_index("ix_offering_images_offering_id", "offering_images", ["offering_id"])
    op.create_index("ix_offering_images_tenant_id", "offering_images", ["tenant_id"])


def _create_activity_days() -> None:
    op.create_table(
        "offering_activity_days",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "day"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
    )


def _create_settings() -> None:
    op.create_table(
        "tenant_offerings_settings",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("currency", sa.String(3), nullable=False, server_default="NGN"),
        _ts("confirmed_at", nullable=True),
        sa.Column("stale_after_days", sa.Integer(), nullable=False, server_default="7"),
        sa.Column("next_seq", sa.Integer(), nullable=False, server_default="1"),
        _ts("created_at", default=True),
        _ts("updated_at", default=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_tenant_offerings_settings_id", "tenant_offerings_settings", ["id"])
    op.create_index(
        "ix_tenant_offerings_settings_tenant_id",
        "tenant_offerings_settings",
        ["tenant_id"],
        unique=True,
    )


def _create_interests() -> None:
    op.create_table(
        "lead_offering_interests",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("lead_id", sa.Uuid(), nullable=False),
        sa.Column("offering_id", sa.Uuid(), nullable=True),
        sa.Column("ref", sa.String(20), nullable=False),
        sa.Column("name_snapshot", sa.String(120), nullable=False),
        sa.Column("price_mode_snapshot", sa.String(20), nullable=False),
        sa.Column("price_snapshot", sa.BigInteger(), nullable=True),
        sa.Column("price_note_snapshot", sa.String(60), nullable=True),
        sa.Column("qty", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("offered_price", sa.BigInteger(), nullable=True),
        _ts("created_at", default=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["lead_id"], ["leads.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["offering_id"], ["offerings.id"], ondelete="SET NULL"),
        sa.CheckConstraint("qty >= 1", name="ck_lead_offering_interests_qty"),
    )
    op.create_index("ix_lead_offering_interests_id", "lead_offering_interests", ["id"])
    op.create_index(
        "ix_lead_offering_interests_tenant_id", "lead_offering_interests", ["tenant_id"]
    )
    op.create_index("ix_lead_offering_interests_lead_id", "lead_offering_interests", ["lead_id"])


def _create_misses() -> None:
    op.create_table(
        "offering_misses",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("term", sa.String(80), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False, server_default="1"),
        _ts("first_seen_at", default=True),
        _ts("last_seen_at", default=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("tenant_id", "term", name="uq_offering_misses_tenant_term"),
    )
    op.create_index("ix_offering_misses_id", "offering_misses", ["id"])
    op.create_index("ix_offering_misses_tenant_id", "offering_misses", ["tenant_id"])


# Creation order matters for the foreign keys.
_CREATORS = (
    ("offerings", _create_offerings),
    ("offering_images", _create_offering_images),
    ("offering_activity_days", _create_activity_days),
    ("tenant_offerings_settings", _create_settings),
    ("lead_offering_interests", _create_interests),
    ("offering_misses", _create_misses),
)


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    # Plan tables are read on the chat path: fail fast on the lock, don't queue behind it.
    op.execute("SET LOCAL lock_timeout = '5s'")

    # Idempotent: some environments (and tests) build tables from the models first.
    for table, create in _CREATORS:
        if not insp.has_table(table):
            create()

    add_plan_columns(bind)
    set_plan_values(bind)
    rewrite_plan_features(bind)
    backfill_tenants(bind)

    op.execute("ALTER TYPE notif_event ADD VALUE IF NOT EXISTS 'offering_request'")


def downgrade() -> None:
    bind = op.get_bind()
    op.execute("SET LOCAL lock_timeout = '5s'")

    rewrite_plan_features(bind, reverse=True)
    insp = sa.inspect(bind)
    for table in TABLES_DROP_ORDER:
        if insp.has_table(table):
            op.drop_table(table)  # drops its indexes with it
    for table in ("tenant_capabilities", "subscription_plans"):
        for field in PLAN_FIELDS:
            if field in _columns(bind, table):
                op.drop_column(table, field)
