"""Marketer referral program models.

Marketers (a separate identity from tenant users and platform admins) share a code; a tenant that
signs up through it is attributed to the marketer, and the tenant's first successful payment earns
the marketer one fixed reward (integer kobo, NGN), paid in admin-approved batches.

ON DELETE choices. Account deletion (AccountPurgeService.purge_tenant) anonymises the tenant row and
keeps it, so none of these FK actions fires on that path; the purge deletes the attribution and
origin rows explicitly and detaches the reward with an explicit UPDATE (tenant_id -> NULL). The
rules below apply to a real DELETE of a tenant row (operator SQL, tests):
* referral_attributions / signup_origins -> tenants: CASCADE. They only describe the tenant.
* referral_rewards.tenant_id -> tenants: SET NULL (column is nullable). A reward is money owed to
  the marketer, so the row survives; unique(tenant_id) still blocks a second reward while the
  tenant exists, and a purged tenant can never pay again.
* anything -> marketers: RESTRICT. Marketers are suspended, never hard-deleted, so history stays.
* admin ids -> platform_admins: SET NULL, so removing an admin never blocks on history.
* referral_rewards.payout_batch_id -> payout_batches: SET NULL.
* payout_items -> payout_batches: CASCADE (an item has no meaning without its batch).
"""

import enum
from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    event,
    func,
    text,
)
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.orm import Mapped, mapped_column

from app.core.encryption import EncryptedString, hash_for_lookup
from app.db.base import Base, TimestampMixin, UUIDMixin


def _enum(enum_cls: type[enum.Enum], name: str) -> SQLEnum:
    """Native enum storing the lowercase values (matches the migration's labels)."""
    return SQLEnum(enum_cls, name=name, values_callable=lambda x: [e.value for e in x])


class MarketerStatus(str, enum.Enum):
    ACTIVE = "active"
    SUSPENDED = "suspended"


class AttributionSource(str, enum.Enum):
    COOKIE = "cookie"
    FORM = "form"


class RewardStatus(str, enum.Enum):
    PENDING = "pending"  # first payment recorded, inside the hold window
    HELD = "held"  # fraud flag or dispute: needs an admin decision
    PAYABLE = "payable"  # cleared the hold, goes in the next batch
    BATCHED = "batched"  # in a payout batch awaiting approval / payment
    PAID = "paid"
    VOID = "void"


class BatchStatus(str, enum.Enum):
    DRAFT = "draft"
    APPROVED = "approved"
    PAYING = "paying"
    PAID = "paid"
    FAILED = "failed"


class PayoutItemStatus(str, enum.Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    PAID = "paid"
    FAILED = "failed"
    SKIPPED = "skipped"
    AWAITING_MANUAL = "awaiting_manual"


class Marketer(Base, UUIDMixin, TimestampMixin):
    """A person who refers tenants for a reward. Not a tenant user, not a platform admin."""

    __tablename__ = "marketers"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str] = mapped_column(EncryptedString, nullable=False)  # encrypted at rest
    # SHA-256 of the email for lookups, same scheme as users.email_hash.
    email_hash: Mapped[str | None] = mapped_column(
        String(64), unique=True, index=True, nullable=True
    )
    # Bcrypt hash; NULL until the invite is accepted.
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Public referral code, immutable once issued.
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[MarketerStatus] = mapped_column(
        _enum(MarketerStatus, "marketer_status"),
        nullable=False,
        default=MarketerStatus.ACTIVE,
        server_default=MarketerStatus.ACTIVE.value,
        index=True,
    )
    bank_code: Mapped[str | None] = mapped_column(EncryptedString, nullable=True)
    account_number: Mapped[str | None] = mapped_column(EncryptedString, nullable=True)
    account_name: Mapped[str | None] = mapped_column(EncryptedString, nullable=True)
    paystack_recipient_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_by_admin_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("platform_admins.id", ondelete="SET NULL"), nullable=True
    )

    def __repr__(self) -> str:
        return f"<Marketer(id={self.id}, code={self.code}, status={self.status})>"


# Codes are unique case-insensitively (functional index; SQLite supports it too).
Index("uq_marketers_code_lower", func.lower(Marketer.code), unique=True)


@event.listens_for(Marketer, "before_insert")
@event.listens_for(Marketer, "before_update")
def _populate_marketer_email_hash(mapper, connection, target):
    """Auto-populate email_hash from email before every insert/update (as User does)."""
    if target.email:
        target.email_hash = hash_for_lookup(target.email)


class ReferralAttribution(Base, UUIDMixin):
    """Which marketer a tenant signed up through. One per tenant: first link wins."""

    __tablename__ = "referral_attributions"

    tenant_id: Mapped[UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    marketer_id: Mapped[UUID] = mapped_column(
        ForeignKey("marketers.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    source: Mapped[AttributionSource] = mapped_column(
        _enum(AttributionSource, "attribution_source"), nullable=False
    )
    self_referral: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    attributed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # Earliest time a payment dispute was recorded for this tenant. A dispute can arrive before
    # the reward exists (creation failed, reconcile creates it later); the reward is then created
    # held with reason payment_dispute instead of pending.
    disputed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PayoutBatch(Base, UUIDMixin, TimestampMixin):
    """A group of payable rewards prepared on the 1st / 15th and approved by an admin."""

    __tablename__ = "payout_batches"

    period_label: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[BatchStatus] = mapped_column(
        _enum(BatchStatus, "batch_status"),
        nullable=False,
        default=BatchStatus.DRAFT,
        server_default=BatchStatus.DRAFT.value,
        index=True,
    )
    total_kobo: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    approved_by_admin_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("platform_admins.id", ondelete="SET NULL"), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ReferralReward(Base, UUIDMixin, TimestampMixin):
    """The one reward a tenant's first payment earns. Unique per tenant (replay guard)."""

    __tablename__ = "referral_rewards"

    # SET NULL, not CASCADE: the reward is money owed to the marketer and must outlive a purge.
    tenant_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="SET NULL"), nullable=True, unique=True
    )
    marketer_id: Mapped[UUID] = mapped_column(
        ForeignKey("marketers.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    amount_kobo: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[RewardStatus] = mapped_column(
        _enum(RewardStatus, "reward_status"),
        nullable=False,
        default=RewardStatus.PENDING,
        server_default=RewardStatus.PENDING.value,
        index=True,
    )
    converted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payable_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    review_reasons: Mapped[list] = mapped_column(
        JSON, nullable=False, default=list, server_default=text("'[]'")
    )
    payout_batch_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("payout_batches.id", ondelete="SET NULL"), nullable=True, index=True
    )
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    transfer_reference: Mapped[str | None] = mapped_column(String(100), nullable=True)


class PayoutItem(Base, UUIDMixin):
    """One marketer's line in a batch. Per-item idempotency guard for payouts."""

    __tablename__ = "payout_items"
    __table_args__ = (
        UniqueConstraint("batch_id", "marketer_id", name="uq_payout_items_batch_marketer"),
    )

    batch_id: Mapped[UUID] = mapped_column(
        ForeignKey("payout_batches.id", ondelete="CASCADE"), nullable=False, index=True
    )
    marketer_id: Mapped[UUID] = mapped_column(
        ForeignKey("marketers.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    amount_kobo: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[PayoutItemStatus] = mapped_column(
        _enum(PayoutItemStatus, "payout_item_status"),
        nullable=False,
        default=PayoutItemStatus.PENDING,
        server_default=PayoutItemStatus.PENDING.value,
    )
    # Which attempt the current transfer_reference belongs to: attempt 1 uses ref-{batch}-{marketer},
    # attempt N > 1 uses ref-{batch}-{marketer}-r{N} (a retry after a provider-confirmed failure).
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    # The exact rewards this item pays (captured at claim time); amount_kobo is their sum.
    reward_ids: Mapped[list] = mapped_column(
        JSON, nullable=False, default=list, server_default=text("'[]'")
    )
    # When the current attempt was claimed (status -> processing). A retry may only re-send a
    # `processing` item whose claim is stale: a fresh one belongs to a worker that is mid-call.
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    transfer_reference: Mapped[str | None] = mapped_column(String(100), nullable=True, unique=True)
    failure_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    provider_status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SignupOrigin(Base, UUIDMixin):
    """Salted hash of the signup IP + user agent (never the raw values), for fraud signals."""

    __tablename__ = "signup_origins"

    tenant_id: Mapped[UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    origin_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)


class ReferralConfig(Base):
    """Admin overrides of the env defaults (reward_kobo, hold_days, commission_window_days)."""

    __tablename__ = "referral_config"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_by_admin_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("platform_admins.id", ondelete="SET NULL"), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
