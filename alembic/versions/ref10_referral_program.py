"""marketer referral program: marketers, attributions, rewards, payout batches/items, origins, config

Revision ID: ref10referral
Revises: wa20ratingreply
Create Date: 2026-10-28

Schema for the marketer referral program (docs/superpowers/specs/2026-10-28-referral-program-design.md,
section 5.8). Purely additive: seven new tables and five native enum types, no change to any
existing table, no data step. Amounts are integer kobo.

* marketers: encrypted email/bank fields, email_hash for lookups, unique index on lower(code).
* referral_attributions / signup_origins: one row per tenant (unique tenant_id), CASCADE on tenant.
* referral_rewards: one row per tenant (unique tenant_id = the replay guard). tenant_id is
  nullable with ON DELETE SET NULL: the reward is money owed to the marketer and must survive the
  tenant's hard purge instead of blocking it.
* payout_batches, payout_items (unique batch_id + marketer_id, unique transfer_reference),
  referral_config (admin overrides of the env defaults).

Enum labels are explicit lowercase, matching the models' values_callable. Idempotent (every table,
index and enum type is checked first) and reversible (downgrade drops tables, then enum types).
Postgres-native ENUMs are created once with checkfirst and referenced with create_type=False.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ENUM

revision: str = "ref10referral"
down_revision: str | Sequence[str] | None = "wa20ratingreply"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ENUM_LABELS = {
    "marketer_status": ("active", "suspended"),
    "attribution_source": ("cookie", "form"),
    "reward_status": ("pending", "held", "payable", "batched", "paid", "void"),
    "batch_status": ("draft", "approved", "paying", "paid", "failed"),
    "payout_item_status": (
        "pending",
        "processing",
        "paid",
        "failed",
        "skipped",
        "awaiting_manual",
    ),
}
# Drop order: children before parents.
TABLES = (
    "payout_items",
    "referral_rewards",
    "referral_attributions",
    "signup_origins",
    "marketers",
    "payout_batches",
    "referral_config",
)


def _enum(name: str) -> ENUM:
    return ENUM(*ENUM_LABELS[name], name=name, create_type=False)


def _has_table(bind, table: str) -> bool:
    return sa.inspect(bind).has_table(table)


def _index_names(bind, table: str) -> set[str]:
    return {i["name"] for i in sa.inspect(bind).get_indexes(table)}


def _create_indexes(bind, table: str, indexes: dict[str, tuple[list, bool]]) -> None:
    existing = _index_names(bind, table)
    for name, (columns, unique) in indexes.items():
        if name not in existing:
            op.create_index(name, table, columns, unique=unique)


def _id() -> sa.Column:
    return sa.Column("id", sa.Uuid(), primary_key=True, nullable=False)


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    ]


def upgrade() -> None:
    bind = op.get_bind()
    # New tables only, but the FKs take a brief lock on tenants / platform_admins: fail fast.
    op.execute("SET LOCAL lock_timeout = '5s'")

    for name in ENUM_LABELS:
        _enum(name).create(bind, checkfirst=True)

    if not _has_table(bind, "marketers"):
        op.create_table(
            "marketers",
            _id(),
            sa.Column("name", sa.String(255), nullable=False),
            sa.Column("email", sa.Text(), nullable=False),
            sa.Column("email_hash", sa.String(64), nullable=True),
            sa.Column("password_hash", sa.String(255), nullable=True),
            sa.Column("code", sa.String(32), nullable=False),
            sa.Column("status", _enum("marketer_status"), server_default="active", nullable=False),
            sa.Column("bank_code", sa.Text(), nullable=True),
            sa.Column("account_number", sa.Text(), nullable=True),
            sa.Column("account_name", sa.Text(), nullable=True),
            sa.Column("paystack_recipient_code", sa.String(64), nullable=True),
            sa.Column(
                "created_by_admin_id",
                sa.Uuid(),
                sa.ForeignKey("platform_admins.id", ondelete="SET NULL"),
                nullable=True,
            ),
            *_timestamps(),
        )
    _create_indexes(
        bind,
        "marketers",
        {
            "ix_marketers_id": (["id"], False),
            "ix_marketers_email_hash": (["email_hash"], True),
            "ix_marketers_status": (["status"], False),
        },
    )
    if "uq_marketers_code_lower" not in _index_names(bind, "marketers"):
        op.create_index(
            "uq_marketers_code_lower", "marketers", [sa.text("lower(code)")], unique=True
        )

    if not _has_table(bind, "payout_batches"):
        op.create_table(
            "payout_batches",
            _id(),
            sa.Column("period_label", sa.String(64), nullable=False),
            sa.Column("status", _enum("batch_status"), server_default="draft", nullable=False),
            sa.Column("total_kobo", sa.Integer(), server_default="0", nullable=False),
            sa.Column(
                "approved_by_admin_id",
                sa.Uuid(),
                sa.ForeignKey("platform_admins.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
            *_timestamps(),
        )
    _create_indexes(
        bind,
        "payout_batches",
        {
            "ix_payout_batches_id": (["id"], False),
            "ix_payout_batches_period_label": (["period_label"], False),
            "ix_payout_batches_status": (["status"], False),
        },
    )

    if not _has_table(bind, "referral_attributions"):
        op.create_table(
            "referral_attributions",
            _id(),
            sa.Column(
                "tenant_id",
                sa.Uuid(),
                sa.ForeignKey("tenants.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "marketer_id",
                sa.Uuid(),
                sa.ForeignKey("marketers.id", ondelete="RESTRICT"),
                nullable=False,
            ),
            sa.Column("source", _enum("attribution_source"), nullable=False),
            sa.Column("self_referral", sa.Boolean(), server_default=sa.false(), nullable=False),
            sa.Column(
                "attributed_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.UniqueConstraint("tenant_id", name="uq_referral_attributions_tenant_id"),
        )
    _create_indexes(
        bind,
        "referral_attributions",
        {
            "ix_referral_attributions_id": (["id"], False),
            "ix_referral_attributions_marketer_id": (["marketer_id"], False),
        },
    )

    if not _has_table(bind, "referral_rewards"):
        op.create_table(
            "referral_rewards",
            _id(),
            sa.Column(
                "tenant_id",
                sa.Uuid(),
                sa.ForeignKey("tenants.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "marketer_id",
                sa.Uuid(),
                sa.ForeignKey("marketers.id", ondelete="RESTRICT"),
                nullable=False,
            ),
            sa.Column("amount_kobo", sa.Integer(), nullable=False),
            sa.Column("status", _enum("reward_status"), server_default="pending", nullable=False),
            sa.Column("converted_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("payable_after", sa.DateTime(timezone=True), nullable=False),
            sa.Column("review_reasons", sa.JSON(), server_default=sa.text("'[]'"), nullable=False),
            sa.Column(
                "payout_batch_id",
                sa.Uuid(),
                sa.ForeignKey("payout_batches.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("transfer_reference", sa.String(100), nullable=True),
            *_timestamps(),
            sa.UniqueConstraint("tenant_id", name="uq_referral_rewards_tenant_id"),
        )
    _create_indexes(
        bind,
        "referral_rewards",
        {
            "ix_referral_rewards_id": (["id"], False),
            "ix_referral_rewards_marketer_id": (["marketer_id"], False),
            "ix_referral_rewards_status": (["status"], False),
            "ix_referral_rewards_payout_batch_id": (["payout_batch_id"], False),
        },
    )

    if not _has_table(bind, "payout_items"):
        op.create_table(
            "payout_items",
            _id(),
            sa.Column(
                "batch_id",
                sa.Uuid(),
                sa.ForeignKey("payout_batches.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "marketer_id",
                sa.Uuid(),
                sa.ForeignKey("marketers.id", ondelete="RESTRICT"),
                nullable=False,
            ),
            sa.Column("amount_kobo", sa.Integer(), nullable=False),
            sa.Column(
                "status", _enum("payout_item_status"), server_default="pending", nullable=False
            ),
            sa.Column("transfer_reference", sa.String(100), nullable=True),
            sa.Column("failure_reason", sa.String(500), nullable=True),
            sa.Column("provider_status", sa.String(64), nullable=True),
            sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.UniqueConstraint("transfer_reference", name="uq_payout_items_transfer_reference"),
            sa.UniqueConstraint("batch_id", "marketer_id", name="uq_payout_items_batch_marketer"),
        )
    _create_indexes(
        bind,
        "payout_items",
        {
            "ix_payout_items_id": (["id"], False),
            "ix_payout_items_batch_id": (["batch_id"], False),
            "ix_payout_items_marketer_id": (["marketer_id"], False),
        },
    )

    if not _has_table(bind, "signup_origins"):
        op.create_table(
            "signup_origins",
            _id(),
            sa.Column(
                "tenant_id",
                sa.Uuid(),
                sa.ForeignKey("tenants.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("origin_hash", sa.String(64), nullable=False),
            sa.UniqueConstraint("tenant_id", name="uq_signup_origins_tenant_id"),
        )
    _create_indexes(
        bind,
        "signup_origins",
        {
            "ix_signup_origins_id": (["id"], False),
            "ix_signup_origins_origin_hash": (["origin_hash"], False),
        },
    )

    if not _has_table(bind, "referral_config"):
        op.create_table(
            "referral_config",
            sa.Column("key", sa.String(64), primary_key=True, nullable=False),
            sa.Column("value", sa.Integer(), nullable=False),
            sa.Column(
                "updated_by_admin_id",
                sa.Uuid(),
                sa.ForeignKey("platform_admins.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
        )


def downgrade() -> None:
    bind = op.get_bind()
    op.execute("SET LOCAL lock_timeout = '5s'")

    for table in TABLES:
        if _has_table(bind, table):
            op.drop_table(table)  # drops its indexes and constraints with it
    for name in ENUM_LABELS:
        _enum(name).drop(bind, checkfirst=True)
