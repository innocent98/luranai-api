"""remove_is_superuser_and_enforce_tenant_id

Revision ID: b543d4aa755c
Revises: e554dedfccaa
Create Date: 2026-01-16 00:47:08.562832

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b543d4aa755c'
down_revision = 'e554dedfccaa'
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    Remove is_superuser column and enforce tenant_id requirement.

    This migration:
    1. Removes the is_superuser column (redundant with role field)
    2. Makes tenant_id NOT NULL (all users must belong to a tenant)

    Note: We're not modifying the enum in the database. The UserRole enum in the
    model now only has tenant_admin, support_agent, and viewer. Any attempt to
    create a user with super_admin role will fail at the application level.
    """

    # Step 1: Drop is_superuser column
    op.drop_column('users', 'is_superuser')

    # Step 2: Make tenant_id NOT NULL
    # If any NULL tenant_id exists, this will fail - which is correct behavior
    op.alter_column('users', 'tenant_id',
                    existing_type=sa.UUID(),
                    nullable=False)


def downgrade() -> None:
    """
    Restore is_superuser column and allow nullable tenant_id.
    """

    # Make tenant_id nullable again
    op.alter_column('users', 'tenant_id',
                    existing_type=sa.UUID(),
                    nullable=True)

    # Add back is_superuser column
    op.add_column('users', sa.Column('is_superuser', sa.Boolean(),
                                      nullable=False, server_default='false'))
