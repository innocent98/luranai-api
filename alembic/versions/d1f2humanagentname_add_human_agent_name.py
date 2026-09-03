"""add HUMAN_AGENT name to message_role enum

Revision ID: d1f2humanagentname
Revises: 5a7851d2e0c2
Create Date: 2026-09-03

Casing fix. SQLAlchemy's ``Enum(MessageRole)`` persists enum *names*, so it
inserts ``HUMAN_AGENT``. The initial schema created ``message_role`` with the
names ``USER``/``ASSISTANT``/``SYSTEM`` (which match), but migration
``3a83f8e6d080`` added the lowercase *value* ``'human_agent'`` instead of the
name. Every agent-message insert (``send_agent_message`` and the agent
WebSocket) therefore failed with::

    invalid input value for enum message_role: "HUMAN_AGENT"

This adds the correct ``HUMAN_AGENT`` label. The stray, unused lowercase
``human_agent`` is left in place — Postgres cannot drop an enum value, and no
rows use it (every insert that would have used it failed).
"""

from alembic import op


# revision identifiers, used by Alembic.
revision = "d1f2humanagentname"
down_revision = "5a7851d2e0c2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Idempotent so it is safe after a manual hotfix ALTER on staging/prod.
    op.execute("ALTER TYPE message_role ADD VALUE IF NOT EXISTS 'HUMAN_AGENT'")


def downgrade() -> None:
    # Postgres has no ALTER TYPE ... DROP VALUE; nothing to reverse.
    pass
