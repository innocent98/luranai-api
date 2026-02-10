"""Audit logging and analytics models."""

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UUIDMixin


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
