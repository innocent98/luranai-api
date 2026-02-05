"""create_flow_tables

Revision ID: c1d2e3f4g5h6
Revises: b7c8d9e0f1g2
Create Date: 2026-02-04

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = 'c1d2e3f4g5h6'
down_revision = 'b7c8d9e0f1g2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create flows, flow_executions, and flow_templates tables."""

    # Create enum types (use DO block to handle duplicates)
    op.execute("""
        DO $$ BEGIN
            CREATE TYPE flow_status AS ENUM ('draft', 'active', 'paused', 'archived');
        EXCEPTION
            WHEN duplicate_object THEN null;
        END $$;
    """)
    op.execute("""
        DO $$ BEGIN
            CREATE TYPE flow_trigger_type AS ENUM ('manual', 'keyword', 'intent', 'schedule', 'event');
        EXCEPTION
            WHEN duplicate_object THEN null;
        END $$;
    """)
    op.execute("""
        DO $$ BEGIN
            CREATE TYPE flow_execution_status AS ENUM ('pending', 'running', 'completed', 'failed', 'cancelled');
        EXCEPTION
            WHEN duplicate_object THEN null;
        END $$;
    """)

    # Create flows table
    op.create_table(
        'flows',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('tenant_id', sa.Uuid(), nullable=False, comment='Tenant that owns this flow'),
        sa.Column('name', sa.String(length=255), nullable=False, comment='Human-readable flow name'),
        sa.Column('description', sa.Text(), nullable=True, comment='Detailed description of what this flow does'),
        sa.Column('status', postgresql.ENUM('draft', 'active', 'paused', 'archived', name='flow_status', create_type=False), nullable=False, server_default='draft', comment='Current status of the flow'),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='true', comment='Whether this flow can be triggered'),
        sa.Column('trigger_type', postgresql.ENUM('manual', 'keyword', 'intent', 'schedule', 'event', name='flow_trigger_type', create_type=False), nullable=False, comment='How this flow is triggered'),
        sa.Column('trigger_config', sa.JSON(), nullable=False, server_default='{}', comment='Trigger configuration: {keywords, intents, schedule, events}'),
        sa.Column('nodes', sa.JSON(), nullable=False, server_default='{}', comment='Flow node definitions: {nodes: [], edges: [], start_node_id}'),
        sa.Column('priority', sa.Integer(), nullable=False, server_default='0', comment='Execution priority (higher = more important)'),
        sa.Column('version', sa.Integer(), nullable=False, server_default='1', comment='Flow version number'),
        sa.Column('created_by', sa.Uuid(), nullable=True, comment='User who created this flow'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['created_by'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id')
    )

    # Create indexes for flows
    op.create_index(op.f('ix_flows_id'), 'flows', ['id'], unique=False)
    op.create_index(op.f('ix_flows_tenant_id'), 'flows', ['tenant_id'], unique=False)
    op.create_index(op.f('ix_flows_status'), 'flows', ['status'], unique=False)
    op.create_index(op.f('ix_flows_is_active'), 'flows', ['is_active'], unique=False)
    op.create_index('ix_flows_tenant_status', 'flows', ['tenant_id', 'status'], unique=False)
    op.create_index('ix_flows_tenant_active', 'flows', ['tenant_id', 'is_active'], unique=False)
    op.create_index('ix_flows_tenant_priority', 'flows', ['tenant_id', 'priority'], unique=False)

    # Create updated_at trigger for flows
    op.execute("""
        CREATE OR REPLACE FUNCTION update_flows_updated_at()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at = now();
            RETURN NEW;
        END;
        $$ language 'plpgsql';
    """)

    op.execute("""
        CREATE TRIGGER update_flows_updated_at
            BEFORE UPDATE ON flows
            FOR EACH ROW
            EXECUTE FUNCTION update_flows_updated_at();
    """)

    # Create flow_executions table
    op.create_table(
        'flow_executions',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('flow_id', sa.Uuid(), nullable=False, comment='Flow being executed'),
        sa.Column('session_id', sa.Uuid(), nullable=False, comment='Conversation session this execution belongs to'),
        sa.Column('current_node_id', sa.String(length=100), nullable=True, comment='ID of the current node being executed'),
        sa.Column('collected_data', sa.JSON(), nullable=False, server_default='{}', comment='Data collected during flow execution'),
        sa.Column('status', postgresql.ENUM('pending', 'running', 'completed', 'failed', 'cancelled', name='flow_execution_status', create_type=False), nullable=False, server_default='pending', comment='Current execution status'),
        sa.Column('result_data', sa.JSON(), nullable=True, comment='Final execution results'),
        sa.Column('error_message', sa.Text(), nullable=True, comment='Error details if execution failed'),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True, comment='When the execution started'),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True, comment='When the execution completed or failed'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['flow_id'], ['flows.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['session_id'], ['conversation_sessions.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )

    # Create indexes for flow_executions
    op.create_index(op.f('ix_flow_executions_id'), 'flow_executions', ['id'], unique=False)
    op.create_index(op.f('ix_flow_executions_flow_id'), 'flow_executions', ['flow_id'], unique=False)
    op.create_index(op.f('ix_flow_executions_session_id'), 'flow_executions', ['session_id'], unique=False)
    op.create_index(op.f('ix_flow_executions_status'), 'flow_executions', ['status'], unique=False)
    op.create_index('ix_flow_executions_flow_status', 'flow_executions', ['flow_id', 'status'], unique=False)

    # Create updated_at trigger for flow_executions
    op.execute("""
        CREATE OR REPLACE FUNCTION update_flow_executions_updated_at()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at = now();
            RETURN NEW;
        END;
        $$ language 'plpgsql';
    """)

    op.execute("""
        CREATE TRIGGER update_flow_executions_updated_at
            BEFORE UPDATE ON flow_executions
            FOR EACH ROW
            EXECUTE FUNCTION update_flow_executions_updated_at();
    """)

    # Create flow_templates table
    op.create_table(
        'flow_templates',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False, comment='Template name'),
        sa.Column('description', sa.Text(), nullable=True, comment='What this template does'),
        sa.Column('category', sa.String(length=100), nullable=False, comment='Template category: support, sales, onboarding, etc.'),
        sa.Column('template_nodes', sa.JSON(), nullable=False, server_default='{}', comment='Template flow structure: {nodes: [], edges: [], start_node_id}'),
        sa.Column('trigger_config', sa.JSON(), nullable=False, server_default='{}', comment='Default trigger configuration for this template'),
        sa.Column('min_tier', sa.String(length=50), nullable=False, server_default='trial', comment='Minimum subscription tier required: trial, lite, professional, enterprise'),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='true', comment='Whether this template is available for use'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name', name='uq_flow_template_name')
    )

    # Create indexes for flow_templates
    op.create_index(op.f('ix_flow_templates_id'), 'flow_templates', ['id'], unique=False)
    op.create_index(op.f('ix_flow_templates_category'), 'flow_templates', ['category'], unique=False)
    op.create_index(op.f('ix_flow_templates_is_active'), 'flow_templates', ['is_active'], unique=False)

    # Create updated_at trigger for flow_templates
    op.execute("""
        CREATE OR REPLACE FUNCTION update_flow_templates_updated_at()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at = now();
            RETURN NEW;
        END;
        $$ language 'plpgsql';
    """)

    op.execute("""
        CREATE TRIGGER update_flow_templates_updated_at
            BEFORE UPDATE ON flow_templates
            FOR EACH ROW
            EXECUTE FUNCTION update_flow_templates_updated_at();
    """)


def downgrade() -> None:
    """Drop flows, flow_executions, and flow_templates tables."""

    # Drop flow_templates triggers and table
    op.execute("DROP TRIGGER IF EXISTS update_flow_templates_updated_at ON flow_templates")
    op.execute("DROP FUNCTION IF EXISTS update_flow_templates_updated_at()")

    op.drop_index(op.f('ix_flow_templates_is_active'), table_name='flow_templates')
    op.drop_index(op.f('ix_flow_templates_category'), table_name='flow_templates')
    op.drop_index(op.f('ix_flow_templates_id'), table_name='flow_templates')
    op.drop_table('flow_templates')

    # Drop flow_executions triggers and table
    op.execute("DROP TRIGGER IF EXISTS update_flow_executions_updated_at ON flow_executions")
    op.execute("DROP FUNCTION IF EXISTS update_flow_executions_updated_at()")

    op.drop_index('ix_flow_executions_flow_status', table_name='flow_executions')
    op.drop_index(op.f('ix_flow_executions_status'), table_name='flow_executions')
    op.drop_index(op.f('ix_flow_executions_session_id'), table_name='flow_executions')
    op.drop_index(op.f('ix_flow_executions_flow_id'), table_name='flow_executions')
    op.drop_index(op.f('ix_flow_executions_id'), table_name='flow_executions')
    op.drop_table('flow_executions')

    # Drop flows triggers and table
    op.execute("DROP TRIGGER IF EXISTS update_flows_updated_at ON flows")
    op.execute("DROP FUNCTION IF EXISTS update_flows_updated_at()")

    op.drop_index('ix_flows_tenant_priority', table_name='flows')
    op.drop_index('ix_flows_tenant_active', table_name='flows')
    op.drop_index('ix_flows_tenant_status', table_name='flows')
    op.drop_index(op.f('ix_flows_is_active'), table_name='flows')
    op.drop_index(op.f('ix_flows_status'), table_name='flows')
    op.drop_index(op.f('ix_flows_tenant_id'), table_name='flows')
    op.drop_index(op.f('ix_flows_id'), table_name='flows')
    op.drop_table('flows')

    # Drop enum types
    op.execute("DROP TYPE IF EXISTS flow_execution_status")
    op.execute("DROP TYPE IF EXISTS flow_trigger_type")
    op.execute("DROP TYPE IF EXISTS flow_status")
