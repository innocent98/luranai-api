"""create_platform_admins_table

Revision ID: b3b1636cabb3
Revises: 7633630f3323
Create Date: 2026-01-15 10:39:53.924981

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b3b1636cabb3'
down_revision = '7633630f3323'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create platform_admin_role enum if it doesn't exist
    op.execute("""
        DO $$ BEGIN
            CREATE TYPE platform_admin_role AS ENUM ('SUPER_ADMIN', 'ADMIN');
        EXCEPTION
            WHEN duplicate_object THEN null;
        END $$;
    """)

    # Create platform_admins table
    op.execute("""
        CREATE TABLE platform_admins (
            id UUID PRIMARY KEY,
            email VARCHAR(255) UNIQUE NOT NULL,
            hashed_password VARCHAR(255) NOT NULL,
            full_name VARCHAR(255) NOT NULL,
            role platform_admin_role NOT NULL,
            is_active BOOLEAN NOT NULL DEFAULT true,
            last_login_at TIMESTAMP WITH TIME ZONE,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
        )
    """)

    # Create indexes
    op.create_index('ix_platform_admins_id', 'platform_admins', ['id'])
    op.create_index('ix_platform_admins_email', 'platform_admins', ['email'])
    op.create_index('ix_platform_admins_role', 'platform_admins', ['role'])

    # Create updated_at trigger
    op.execute("""
        CREATE OR REPLACE FUNCTION update_platform_admins_updated_at()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at = now();
            RETURN NEW;
        END;
        $$ language 'plpgsql';
    """)

    op.execute("""
        CREATE TRIGGER update_platform_admins_updated_at
            BEFORE UPDATE ON platform_admins
            FOR EACH ROW
            EXECUTE FUNCTION update_platform_admins_updated_at();
    """)


def downgrade() -> None:
    # Drop trigger and function
    op.execute("DROP TRIGGER IF EXISTS update_platform_admins_updated_at ON platform_admins")
    op.execute("DROP FUNCTION IF EXISTS update_platform_admins_updated_at()")

    # Drop indexes
    op.drop_index('ix_platform_admins_role', 'platform_admins')
    op.drop_index('ix_platform_admins_email', 'platform_admins')
    op.drop_index('ix_platform_admins_id', 'platform_admins')

    # Drop table
    op.drop_table('platform_admins')

    # Drop enum
    sa.Enum(name='platform_admin_role').drop(op.get_bind())
