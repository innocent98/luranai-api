"""create leads table; add FK audit_reports.lead_id → leads.id

Revision ID: d5e6f7g8h9i0
Revises: c4d5e6f7g8h9
Create Date: 2026-06-19 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "d5e6f7g8h9i0"
down_revision = "c4d5e6f7g8h9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- leads ---
    op.create_table(
        "leads",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=True),
        # PII — stored as Fernet-encrypted text
        sa.Column("name", sa.Text, nullable=True),
        sa.Column("email", sa.Text, nullable=True),
        sa.Column("email_hash", sa.String(64), nullable=True),
        sa.Column("company", sa.String(255), nullable=True),
        sa.Column("team_size", sa.String(50), nullable=True),
        sa.Column("industry", sa.String(120), nullable=True),
        sa.Column("intent", sa.String(255), nullable=True),
        sa.Column("lead_score", sa.Integer, nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "new", "contacted", "qualified", "converted",
                name="lead_status",
            ),
            nullable=False,
            server_default="new",
        ),
        sa.Column("source", sa.String(80), nullable=True),
        sa.Column("utm", sa.JSON, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("crm_id", sa.String(255), nullable=True),
        sa.Column("meeting_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "audit_report_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("audit_reports.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
    )
    op.create_index("ix_leads_tenant_id", "leads", ["tenant_id"])
    op.create_index("ix_leads_email_hash", "leads", ["email_hash"])
    op.create_index(
        "ix_leads_tenant_email_hash", "leads", ["tenant_id", "email_hash"]
    )

    # Close the forward-reference FK from Task 4:
    # audit_reports.lead_id → leads.id  (was plain UUID, no FK)
    op.create_foreign_key(
        "fk_audit_reports_lead_id",
        "audit_reports",
        "leads",
        ["lead_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_audit_reports_lead_id", "audit_reports", type_="foreignkey"
    )

    op.drop_index("ix_leads_tenant_email_hash", table_name="leads")
    op.drop_index("ix_leads_email_hash", table_name="leads")
    op.drop_index("ix_leads_tenant_id", table_name="leads")
    op.drop_table("leads")

    op.execute("DROP TYPE IF EXISTS lead_status")
