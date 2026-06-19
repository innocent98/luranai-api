"""create audit_reports and audit_entries staging tables; make processing_jobs.tenant_id nullable

Revision ID: c4d5e6f7g8h9
Revises: b3c4d5e6f7g8
Create Date: 2026-06-19 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "c4d5e6f7g8h9"
down_revision = "b3c4d5e6f7g8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- audit_reports ---
    op.create_table(
        "audit_reports",
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
        sa.Column("url", sa.String(2048), nullable=False),
        sa.Column("domain", sa.String(255), nullable=False),
        sa.Column(
            "scope",
            sa.Enum("public", "dashboard", name="audit_scope"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "queued", "crawling", "analyzing", "completed", "failed",
                name="audit_status",
            ),
            nullable=False,
            server_default="queued",
        ),
        sa.Column("pages_discovered", sa.Integer, nullable=False, server_default="0"),
        sa.Column("pages_scanned", sa.Integer, nullable=False, server_default="0"),
        sa.Column(
            "business_profile_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("business_profiles.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("knowledge_score", sa.Integer, nullable=True),
        sa.Column("support_score", sa.Integer, nullable=True),
        sa.Column("conversion_score", sa.Integer, nullable=True),
        sa.Column("overall_score", sa.Integer, nullable=True),
        sa.Column("projected_score", sa.Integer, nullable=True),
        sa.Column("gaps", sa.JSON, nullable=False, server_default=sa.text("'[]'")),
        sa.Column(
            "summary_stats", sa.JSON, nullable=False, server_default=sa.text("'{}'"
        )),
        sa.Column("imported", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=True),
        # email stored as encrypted text (PII)
        sa.Column("email", sa.Text, nullable=True),
        sa.Column("email_hash", sa.String(64), nullable=True),
        # lead_id: plain UUID; FK added in Task 5 migration
        sa.Column("lead_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("claim_token", sa.String(64), nullable=True),
        sa.Column(
            "processing_job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("processing_jobs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
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
    op.create_index(
        "ix_audit_reports_tenant_id", "audit_reports", ["tenant_id"]
    )
    op.create_index(
        "ix_audit_reports_status", "audit_reports", ["status"]
    )
    op.create_index(
        "ix_audit_reports_tenant_status",
        "audit_reports",
        ["tenant_id", "status"],
    )
    op.create_index(
        "ix_audit_reports_claim_token", "audit_reports", ["claim_token"]
    )
    op.create_index(
        "ix_audit_reports_email_hash", "audit_reports", ["email_hash"]
    )

    # --- audit_entries ---
    op.create_table(
        "audit_entries",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "audit_report_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("audit_reports.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("url", sa.String(2048), nullable=False),
        sa.Column("title", sa.String(500), nullable=True),
        sa.Column("category", sa.String(255), nullable=True),
        sa.Column(
            "kind",
            sa.Enum(
                "faq", "policy", "product", "pricing", "guide",
                "contact", "testimonial", "other",
                name="audit_entry_kind",
            ),
            nullable=False,
            server_default="other",
        ),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("importance_rank", sa.Integer, nullable=False, server_default="0"),
        sa.Column(
            "status",
            sa.Enum(
                "proposed", "active", "locked", "saved", "imported", "discarded",
                name="audit_entry_status",
            ),
            nullable=False,
            server_default="proposed",
        ),
        sa.Column(
            "knowledge_source_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("knowledge_sources.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("meta", sa.JSON, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("edited", sa.Boolean, nullable=False, server_default="false"),
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
    op.create_index(
        "ix_audit_entries_audit_report_id", "audit_entries", ["audit_report_id"]
    )
    op.create_index(
        "ix_audit_entries_tenant_id", "audit_entries", ["tenant_id"]
    )
    op.create_index(
        "ix_audit_entries_status", "audit_entries", ["status"]
    )
    op.create_index(
        "ix_audit_entries_content_hash", "audit_entries", ["content_hash"]
    )
    op.create_index(
        "ix_audit_entries_report_status",
        "audit_entries",
        ["audit_report_id", "status"],
    )
    op.create_index(
        "ix_audit_entries_report_hash",
        "audit_entries",
        ["audit_report_id", "content_hash"],
    )

    # --- make processing_jobs.tenant_id nullable ---
    op.alter_column(
        "processing_jobs",
        "tenant_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=True,
    )


def downgrade() -> None:
    # restore processing_jobs.tenant_id NOT NULL
    op.alter_column(
        "processing_jobs",
        "tenant_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=False,
    )

    op.drop_index("ix_audit_entries_report_hash", table_name="audit_entries")
    op.drop_index("ix_audit_entries_report_status", table_name="audit_entries")
    op.drop_index("ix_audit_entries_content_hash", table_name="audit_entries")
    op.drop_index("ix_audit_entries_status", table_name="audit_entries")
    op.drop_index("ix_audit_entries_tenant_id", table_name="audit_entries")
    op.drop_index("ix_audit_entries_audit_report_id", table_name="audit_entries")
    op.drop_table("audit_entries")

    op.drop_index("ix_audit_reports_email_hash", table_name="audit_reports")
    op.drop_index("ix_audit_reports_claim_token", table_name="audit_reports")
    op.drop_index("ix_audit_reports_tenant_status", table_name="audit_reports")
    op.drop_index("ix_audit_reports_status", table_name="audit_reports")
    op.drop_index("ix_audit_reports_tenant_id", table_name="audit_reports")
    op.drop_table("audit_reports")

    op.execute("DROP TYPE IF EXISTS audit_entry_status")
    op.execute("DROP TYPE IF EXISTS audit_entry_kind")
    op.execute("DROP TYPE IF EXISTS audit_status")
    op.execute("DROP TYPE IF EXISTS audit_scope")
