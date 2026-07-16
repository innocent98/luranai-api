"""Realign plan limits: auditor crawl depth + knowledge-source budgets

Trial/Lite crawl depth comes down (a trial that scans 50 pages but can only
import 2 sources reads as a bait-and-switch), and Lite/Growth get more room in
the knowledge base they actually pay for:

    trial  max_audit_pages_per_crawl  50 -> 20
    lite   max_audit_pages_per_crawl 200 -> 100
    lite   max_knowledge_sources      15 -> 20
    growth max_knowledge_sources      50 -> 100

Enterprise needs no data change: its caps are already NULL. NULL means "no plan
limit" everywhere else, and pipeline._max_pages now reads it that way too
(it used to fall through to the 500 default, so Enterprise silently crawled no
deeper than Growth).

Two tables move together. subscription_plans is the source of truth applied on
tier change (subscription_service._update_capabilities_for_tier), but existing
tenants already hold materialised tenant_capabilities rows that no tier change
would revisit — without the second update, current customers keep the old
limits indefinitely.

Manual overrides are preserved: _update_capabilities_for_tier skips any field
listed in override_flags, so a platform admin who granted a custom limit keeps
it. This migration honours the same contract.

Revision ID: b7d21c9f3a04
Revises: f8019e3a99e5
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7d21c9f3a04"
down_revision: str | Sequence[str] | None = "f8019e3a99e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# (tier, column, new_value, old_value)
_CHANGES = [
    ("trial", "max_audit_pages_per_crawl", 20, 50),
    ("lite", "max_audit_pages_per_crawl", 100, 200),
    ("lite", "max_knowledge_sources", 20, 15),
    ("growth", "max_knowledge_sources", 100, 50),
]


def _apply(tier: str, column: str, value: int, previous: int) -> None:
    """Set *column* to *value* for *tier* in both plan and capability tables."""
    op.execute(
        sa.text(
            f"UPDATE subscription_plans SET {column} = :value WHERE tier = :tier"  # noqa: S608
        ).bindparams(value=value, tier=tier)
    )
    # Only move tenants still sitting on the previous value. A tenant with some
    # other number was set deliberately (admin grant, promo, hand-fix) and is
    # not ours to overwrite — the override_flags check below covers the tracked
    # case, this covers untracked ones.
    op.execute(
        sa.text(
            f"""
            UPDATE tenant_capabilities tc
               SET {column} = :value
              FROM tenants t
             WHERE t.id = tc.tenant_id
               AND t.subscription_tier = :tier
               AND tc.{column} IS NOT DISTINCT FROM :previous
               AND NOT (COALESCE(tc.override_flags, '{{}}')::jsonb ? :column)
            """  # noqa: S608
        ).bindparams(value=value, tier=tier, previous=previous, column=column)
    )


def upgrade() -> None:
    for tier, column, new, old in _CHANGES:
        _apply(tier, column, new, old)


def downgrade() -> None:
    for tier, column, new, old in _CHANGES:
        _apply(tier, column, old, new)
