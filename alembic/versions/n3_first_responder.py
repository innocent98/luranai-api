"""tenant_ai_configs.first_responder (N3 who answers first)

Revision ID: n3firstresp
Revises: mp70attachpos
Create Date: 2026-10-10

Adds first_responder (default 'ai' = today's behaviour) and moves tenants
with zero ACTIVE knowledge sources to 'human'. source_status stores enum
NAMES (uppercase), hence 'ACTIVE'. Idempotent: only rows still 'ai' with no
ACTIVE source are touched. Logs the affected count only (no tenant data).
"""

import logging

import sqlalchemy as sa
from alembic import op

revision = "n3firstresp"
down_revision = "mp70attachpos"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")

N3_DATA_STEP_SQL = (
    "UPDATE tenant_ai_configs SET first_responder = 'human' "
    "WHERE first_responder = 'ai' AND NOT EXISTS ("
    "  SELECT 1 FROM knowledge_sources ks "
    "  WHERE ks.tenant_id = tenant_ai_configs.tenant_id "
    "  AND CAST(ks.status AS TEXT) = 'ACTIVE')"
)


def upgrade() -> None:
    op.add_column(
        "tenant_ai_configs",
        sa.Column(
            "first_responder",
            sa.String(20),
            nullable=False,
            server_default="ai",
            comment="Who answers first: ai, ai_with_handoff, human (N3)",
        ),
    )
    result = op.get_bind().execute(sa.text(N3_DATA_STEP_SQL))
    logger.info("n3firstresp: %s tenant(s) set to first_responder=human", result.rowcount)


def downgrade() -> None:
    op.drop_column("tenant_ai_configs", "first_responder")
