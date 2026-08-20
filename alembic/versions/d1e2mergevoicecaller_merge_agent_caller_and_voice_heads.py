"""merge agent-caller and voice-chat migration heads

Revision ID: d1e2mergevoicecaller
Revises: ac30agentcaller, c1a0
Create Date: 2026-08-20 15:30:00.000000
"""

# This is an Alembic merge revision with no schema changes.
# Both feature branches (agent-caller push, voice chat) independently branched
# their migration off the same parent (ab20blockedquality), creating two heads
# once both merged. This unifies them so `alembic upgrade head` stays linear.

revision = "d1e2mergevoicecaller"
down_revision = ("ac30agentcaller", "c1a0")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
