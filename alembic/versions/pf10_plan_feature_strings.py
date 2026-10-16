"""Plan feature strings: match the realigned knowledge-source limits

b7d21c9f3a04 raised Lite to 20 knowledge sources and Growth to 100, but only
touched the capability columns. The marketing strings in
subscription_plans.features kept the old numbers, so GET /subscription/plans
told customers "15 knowledge sources" (Lite) and "50 knowledge sources"
(Growth) while enforcing 20 and 100:

    lite    "15 knowledge sources" -> "20 knowledge sources"
    growth  "50 knowledge sources" -> "100 knowledge sources"

Only an exact match of the old string is replaced, so a plan whose features a
platform admin has since edited by hand is left alone. Running it twice is a
no-op.

Revision ID: pf10planfeatures
Revises: n2replyto
"""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "pf10planfeatures"
down_revision: str | Sequence[str] | None = "n2replyto"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# (tier, old_string, new_string)
FEATURE_CHANGES = [
    ("lite", "15 knowledge sources", "20 knowledge sources"),
    ("growth", "50 knowledge sources", "100 knowledge sources"),
]


def rewrite_features(features: list[str], old: str, new: str) -> list[str] | None:
    """Return *features* with *old* swapped for *new*, or None if nothing changed."""
    if old not in features:
        return None
    return [new if item == old else item for item in features]


def apply_changes(bind, reverse: bool = False) -> int:
    """Rewrite the feature strings on *bind*; return how many plans changed."""
    changed = 0
    for tier, old, new in FEATURE_CHANGES:
        if reverse:
            old, new = new, old
        raw = bind.execute(
            sa.text("SELECT features FROM subscription_plans WHERE tier = :tier"),
            {"tier": tier},
        ).scalar()
        if raw is None:
            continue
        features = json.loads(raw) if isinstance(raw, str) else list(raw)
        updated = rewrite_features(features, old, new)
        if updated is None:
            continue
        bind.execute(
            sa.text(
                "UPDATE subscription_plans SET features = CAST(:features AS JSON) WHERE tier = :tier"
            ),
            {"features": json.dumps(updated), "tier": tier},
        )
        changed += 1
    return changed


def upgrade() -> None:
    apply_changes(op.get_bind())


def downgrade() -> None:
    apply_changes(op.get_bind(), reverse=True)
