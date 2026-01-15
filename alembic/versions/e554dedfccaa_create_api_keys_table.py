"""Create API keys table

Revision ID: e554dedfccaa
Revises: 217d738d44bc
Create Date: 2026-01-15 13:42:26.345131

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'e554dedfccaa'
down_revision = '217d738d44bc'
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create api_keys table for widget authentication."""
    # Create api_keys table (SQLAlchemy will create the enum automatically)
    op.create_table('api_keys',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('tenant_id', sa.Uuid(), nullable=False, comment='Tenant this API key belongs to'),
        sa.Column('key_type', sa.Enum('PUBLIC', 'SECRET', name='api_key_type'), nullable=False, comment='Type of API key (public or secret)'),
        sa.Column('key_prefix', sa.String(length=20), nullable=False, comment="Key prefix for display (e.g., 'pk_live_abc...')"),
        sa.Column('key_hash', sa.String(length=255), nullable=False, comment='Hashed full API key'),
        sa.Column('name', sa.String(length=100), nullable=False, comment='Descriptive name for this API key'),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='true', comment='Whether this API key is active'),
        sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True, comment='Last time this API key was used'),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True, comment='Expiration date (null = never expires)'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )

    # Create indexes
    op.create_index(op.f('ix_api_keys_id'), 'api_keys', ['id'], unique=False)
    op.create_index(op.f('ix_api_keys_tenant_id'), 'api_keys', ['tenant_id'], unique=False)
    op.create_index(op.f('ix_api_keys_key_type'), 'api_keys', ['key_type'], unique=False)
    op.create_index(op.f('ix_api_keys_key_hash'), 'api_keys', ['key_hash'], unique=True)
    op.create_index(op.f('ix_api_keys_is_active'), 'api_keys', ['is_active'], unique=False)

    # Create updated_at trigger
    op.execute("""
        CREATE OR REPLACE FUNCTION update_api_keys_updated_at()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at = now();
            RETURN NEW;
        END;
        $$ language 'plpgsql';
    """)

    op.execute("""
        CREATE TRIGGER update_api_keys_updated_at
            BEFORE UPDATE ON api_keys
            FOR EACH ROW
            EXECUTE FUNCTION update_api_keys_updated_at();
    """)


def downgrade() -> None:
    """Drop api_keys table."""
    op.drop_index(op.f('ix_api_keys_is_active'), table_name='api_keys')
    op.drop_index(op.f('ix_api_keys_key_hash'), table_name='api_keys')
    op.drop_index(op.f('ix_api_keys_key_type'), table_name='api_keys')
    op.drop_index(op.f('ix_api_keys_tenant_id'), table_name='api_keys')
    op.drop_index(op.f('ix_api_keys_id'), table_name='api_keys')
    op.drop_table('api_keys')
    op.execute("DROP TYPE IF EXISTS api_key_type")
