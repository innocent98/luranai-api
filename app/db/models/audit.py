"""Audit logging and analytics models."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.encryption import EncryptedString
from app.db.base import Base, TimestampMixin, UUIDMixin


class AuditLog(Base, UUIDMixin):
    """
    Tracks all significant actions in the system.

    Provides comprehensive audit trail for security, compliance,
    and debugging purposes.
    """

    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_tenant_created", "tenant_id", "created_at"),
        Index("ix_audit_logs_user_created", "user_id", "created_at"),
        Index("ix_audit_logs_action", "action"),
        Index("ix_audit_logs_resource", "resource_type", "resource_id"),
    )

    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
        comment="Tenant this action relates to (null for platform-level actions)",
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="User who performed this action (null for system actions)",
    )
    action: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        comment="Action performed (e.g., 'knowledge_upload', 'config_update')",
    )
    resource_type: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        comment="Type of resource affected (e.g., 'tenant', 'knowledge_source')",
    )
    resource_id: Mapped[uuid.UUID | None] = mapped_column(
        nullable=True,
        comment="ID of the affected resource",
    )
    changes: Mapped[dict | None] = mapped_column(
        JSON,
        nullable=True,
        comment="Before/after values for the change",
    )
    ip_address: Mapped[str | None] = mapped_column(
        String(45),
        nullable=True,
        comment="IP address of the request (IPv4 or IPv6)",
    )
    user_agent: Mapped[str | None] = mapped_column(
        String(500),
        nullable=True,
        comment="User agent string from the request",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=datetime.utcnow,
        index=True,
        comment="When this action occurred",
    )

    def __repr__(self) -> str:
        return f"<AuditLog(id={self.id}, action={self.action}, resource_type={self.resource_type})>"


class AnalyticsMetric(Base, UUIDMixin):
    """
    Pre-aggregated metrics for analytics dashboard.

    Stores computed metrics over time periods to avoid expensive
    real-time calculations.
    """

    __tablename__ = "analytics_metrics"
    __table_args__ = (
        Index(
            "ix_analytics_metrics_tenant_type_period", "tenant_id", "metric_type", "period_start"
        ),
        Index("ix_analytics_metrics_period", "period_start", "period_end"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
        comment="Tenant this metric belongs to",
    )
    metric_type: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        index=True,
        comment="Type of metric (e.g., 'conversation_count', 'escalation_rate')",
    )
    metric_value: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        comment="Computed metric value",
    )
    meta: Mapped[dict | None] = mapped_column(
        JSON,
        nullable=True,
        comment="Additional dimensions or breakdown",
    )
    period_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
        comment="Start of the measurement period",
    )
    period_end: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
        comment="End of the measurement period",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=datetime.utcnow,
        comment="When this metric was computed",
    )

    def __repr__(self) -> str:
        return f"<AnalyticsMetric(id={self.id}, tenant_id={self.tenant_id}, metric_type={self.metric_type}, value={self.metric_value})>"


# ---------------------------------------------------------------------------
# Website Auditor staging models
# ---------------------------------------------------------------------------


class AuditScope(str, enum.Enum):
    PUBLIC = "public"
    DASHBOARD = "dashboard"


class AuditStatus(str, enum.Enum):
    QUEUED = "queued"
    CRAWLING = "crawling"
    ANALYZING = "analyzing"
    COMPLETED = "completed"
    FAILED = "failed"


class ScanQuality(str, enum.Enum):
    OK = "ok"
    THIN = "thin"
    UNREACHABLE = "unreachable"
    RENDER_FAILED = "render_failed"


class AuditEntryStatus(str, enum.Enum):
    PROPOSED = "proposed"
    ACTIVE = "active"
    LOCKED = "locked"
    SAVED = "saved"
    IMPORTED = "imported"
    DISCARDED = "discarded"


class AuditEntryKind(str, enum.Enum):
    FAQ = "faq"
    POLICY = "policy"
    PRODUCT = "product"
    PRICING = "pricing"
    GUIDE = "guide"
    CONTACT = "contact"
    TESTIMONIAL = "testimonial"
    OTHER = "other"


def _audit_enum(e, name):
    return SQLEnum(e, name=name, values_callable=lambda x: [m.value for m in x])


class AuditReport(Base, UUIDMixin, TimestampMixin):
    """Per-audit report: holds crawled summary, scores, gaps, and claim token."""

    __tablename__ = "audit_reports"
    __table_args__ = (
        Index("ix_audit_reports_tenant_status", "tenant_id", "status"),
        Index("ix_audit_reports_claim_token", "claim_token"),
    )

    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=True, index=True
    )
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    domain: Mapped[str] = mapped_column(String(255), nullable=False)
    scope: Mapped[AuditScope] = mapped_column(
        _audit_enum(AuditScope, "audit_scope"), nullable=False
    )
    status: Mapped[AuditStatus] = mapped_column(
        _audit_enum(AuditStatus, "audit_status"),
        default=AuditStatus.QUEUED,
        nullable=False,
        index=True,
    )
    pages_discovered: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    pages_scanned: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    business_profile_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("business_profiles.id", ondelete="SET NULL"), nullable=True
    )
    knowledge_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    support_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    conversion_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    overall_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    projected_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    gaps: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    summary_stats: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    imported: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    imported_at: Mapped["datetime | None"] = mapped_column(DateTime(timezone=True), nullable=True)
    email: Mapped[str | None] = mapped_column(EncryptedString, nullable=True)
    email_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # lead_id is a plain UUID here; FK constraint added in Task 5's migration
    lead_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    claim_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    processing_job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("processing_jobs.id", ondelete="SET NULL"), nullable=True
    )
    completed_at: Mapped["datetime | None"] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    scan_quality: Mapped[ScanQuality | None] = mapped_column(
        _audit_enum(ScanQuality, "audit_scan_quality"), nullable=True, default=None
    )
    scan_quality_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)

    entries: Mapped[list["AuditEntry"]] = relationship(
        "AuditEntry", back_populates="report", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<AuditReport(id={self.id}, domain={self.domain}, status={self.status})>"


class AuditEntry(Base, UUIDMixin, TimestampMixin):
    """Staged extracted content from a crawled page, awaiting review or import."""

    __tablename__ = "audit_entries"
    __table_args__ = (
        Index("ix_audit_entries_report_status", "audit_report_id", "status"),
        Index("ix_audit_entries_report_hash", "audit_report_id", "content_hash"),
    )

    audit_report_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("audit_reports.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=True, index=True
    )
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    category: Mapped[str | None] = mapped_column(String(255), nullable=True)
    kind: Mapped[AuditEntryKind] = mapped_column(
        _audit_enum(AuditEntryKind, "audit_entry_kind"),
        default=AuditEntryKind.OTHER,
        nullable=False,
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    importance_rank: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[AuditEntryStatus] = mapped_column(
        _audit_enum(AuditEntryStatus, "audit_entry_status"),
        default=AuditEntryStatus.PROPOSED,
        nullable=False,
        index=True,
    )
    knowledge_source_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("knowledge_sources.id", ondelete="SET NULL"), nullable=True
    )
    meta: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    edited: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    report: Mapped["AuditReport"] = relationship("AuditReport", back_populates="entries")

    def __repr__(self) -> str:
        return f"<AuditEntry(id={self.id}, kind={self.kind}, status={self.status})>"
