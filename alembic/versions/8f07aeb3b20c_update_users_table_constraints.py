"""update_users_table_constraints

Revision ID: 8f07aeb3b20c
Revises: b5f5a3a45ea2
Create Date: 2026-01-15 10:55:04.987503

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '8f07aeb3b20c'
down_revision = 'b5f5a3a45ea2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Make tenant_id NOT NULL (all tenant users must belong to a tenant)
    op.alter_column('users', 'tenant_id', nullable=False)

    # Add check constraint to ensure only tenant roles are used
    op.execute("""
        ALTER TABLE users ADD CONSTRAINT check_tenant_user_roles
        CHECK (role IN ('TENANT_ADMIN', 'SUPPORT_AGENT', 'VIEWER'))
    """)


def downgrade() -> None:
    # Remove check constraint
    op.execute("ALTER TABLE users DROP CONSTRAINT IF EXISTS check_tenant_user_roles")

    # Make tenant_id nullable again
    op.alter_column('users', 'tenant_id', nullable=True)
