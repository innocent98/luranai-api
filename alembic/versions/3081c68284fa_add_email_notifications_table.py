"""add_email_notifications_table

Revision ID: 3081c68284fa
Revises: f3d55906a9b1
Create Date: 2026-01-27 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = '3081c68284fa'
down_revision = 'f3d55906a9b1'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create email_notifications table
    op.create_table(
        'email_notifications',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False, primary_key=True, index=True),
        sa.Column('tenant_id', postgresql.UUID(as_uuid=True), nullable=False, index=True),
        sa.Column('notification_type', sa.String(length=50), nullable=False, index=True, comment='Type: trial_expiring, subscription_confirmed, payment_failed, etc.'),
        sa.Column('recipient_email', sa.String(length=255), nullable=False, comment='Email address of recipient'),
        sa.Column('subject', sa.String(length=500), nullable=False, comment='Email subject line'),
        sa.Column('reference_id', postgresql.UUID(as_uuid=True), nullable=True, comment='Payment ID, subscription ID, etc.'),
        sa.Column('reference_type', sa.String(length=50), nullable=True, comment='Type of reference: payment, subscription, etc.'),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='pending', index=True, comment='Status: pending, sent, failed, skipped'),
        sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True, comment='When the email was successfully sent'),
        sa.Column('error_message', sa.Text(), nullable=True, comment='Error message if sending failed'),
        sa.Column('template_data', postgresql.JSON(astext_type=sa.Text()), nullable=True, comment='Template variables used in the email'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('NOW()'), comment='When this record was created'),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('NOW()'), comment='When this record was last updated'),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )

    # Create composite indexes for better query performance
    op.create_index(
        'ix_email_notifications_tenant_type',
        'email_notifications',
        ['tenant_id', 'notification_type']
    )
    op.create_index(
        'ix_email_notifications_status_created',
        'email_notifications',
        ['status', 'created_at']
    )
    op.create_index(
        'ix_email_notifications_reference',
        'email_notifications',
        ['reference_type', 'reference_id']
    )


def downgrade() -> None:
    # Drop indexes first
    op.drop_index('ix_email_notifications_reference', table_name='email_notifications')
    op.drop_index('ix_email_notifications_status_created', table_name='email_notifications')
    op.drop_index('ix_email_notifications_tenant_type', table_name='email_notifications')

    # Drop table
    op.drop_table('email_notifications')
