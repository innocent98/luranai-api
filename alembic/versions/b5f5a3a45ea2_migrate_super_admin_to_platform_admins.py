"""migrate_super_admin_to_platform_admins

Revision ID: b5f5a3a45ea2
Revises: b3b1636cabb3
Create Date: 2026-01-15 10:53:11.345964

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b5f5a3a45ea2'
down_revision = 'b3b1636cabb3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Copy SUPER_ADMIN users from users table to platform_admins table
    op.execute("""
        INSERT INTO platform_admins (id, email, hashed_password, full_name, role, is_active, last_login_at, created_at, updated_at)
        SELECT id, email, hashed_password, full_name, 'SUPER_ADMIN'::platform_admin_role, is_active, last_login_at, created_at, updated_at
        FROM users
        WHERE role = 'SUPER_ADMIN' AND tenant_id IS NULL
        ON CONFLICT (email) DO NOTHING
    """)

    # Delete SUPER_ADMIN users from users table
    op.execute("""
        DELETE FROM users
        WHERE role = 'SUPER_ADMIN' AND tenant_id IS NULL
    """)


def downgrade() -> None:
    # Copy platform admins back to users table
    op.execute("""
        INSERT INTO users (id, email, hashed_password, full_name, role, tenant_id, is_active, is_superuser, last_login_at, created_at, updated_at)
        SELECT id, email, hashed_password, full_name, 'SUPER_ADMIN'::user_role, NULL, is_active, true, last_login_at, created_at, updated_at
        FROM platform_admins
        WHERE role = 'SUPER_ADMIN'
        ON CONFLICT (email) DO NOTHING
    """)

    # Delete from platform_admins
    op.execute("""
        DELETE FROM platform_admins
        WHERE role = 'SUPER_ADMIN'
    """)
