"""Flow and workflow automation models."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime
from sqlalchemy import Enum as SQLEnum
from sqlalchemy import ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDMixin


class FlowStatus(str, enum.Enum):
    """Flow status enum."""

    DRAFT = "draft"
    ACTIVE = "active"
    PAUSED = "paused"
    ARCHIVED = "archived"


class FlowTriggerType(str, enum.Enum):
    """Flow trigger type enum."""

    MANUAL = "manual"
    KEYWORD = "keyword"
    INTENT = "intent"
    SCHEDULE = "schedule"
    EVENT = "event"


class FlowExecutionStatus(str, enum.Enum):
    """Flow execution status enum."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Flow(Base, UUIDMixin, TimestampMixin):
    """
    Workflow/flow definition for automated conversation handling.

    Represents a configured workflow that can be triggered during conversations
    to handle specific scenarios with predefined logic and steps.
    """

    __tablename__ = "flows"
    __table_args__ = (
        Index("ix_flows_tenant_status", "tenant_id", "status"),
        Index("ix_flows_tenant_active", "tenant_id", "is_active"),
        Index("ix_flows_tenant_priority", "tenant_id", "priority"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
        comment="Tenant that owns this flow",
    )
    name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="Human-readable flow name",
    )
    description: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Detailed description of what this flow does",
    )
    status: Mapped[FlowStatus] = mapped_column(
        SQLEnum(FlowStatus, name="flow_status", values_callable=lambda x: [e.value for e in x]),
        default=FlowStatus.DRAFT,
        nullable=False,
        index=True,
        comment="Current status of the flow",
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
        index=True,
        comment="Whether this flow can be triggered",
    )
    trigger_type: Mapped[FlowTriggerType] = mapped_column(
        SQLEnum(
            FlowTriggerType,
            name="flow_trigger_type",
            values_callable=lambda x: [e.value for e in x],
        ),
        nullable=False,
        comment="How this flow is triggered",
    )
    trigger_config: Mapped[dict] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
        comment="Trigger configuration: {keywords, intents, schedule, events}",
    )
    nodes: Mapped[dict] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
        comment="Flow node definitions: {nodes: [], edges: [], start_node_id}",
    )
    priority: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
        comment="Execution priority (higher = more important)",
    )
    version: Mapped[int] = mapped_column(
        Integer,
        default=1,
        nullable=False,
        comment="Flow version number",
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        comment="User who created this flow",
    )

    # Relationships
    executions: Mapped[list["FlowExecution"]] = relationship(
        "FlowExecution",
        back_populates="flow",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<Flow(id={self.id}, name={self.name}, status={self.status})>"


class FlowExecution(Base, UUIDMixin, TimestampMixin):
    """
    Individual execution instance of a flow.

    Tracks the runtime state and results of a flow execution
    within a conversation session.
    """

    __tablename__ = "flow_executions"
    __table_args__ = (
        Index("ix_flow_executions_flow_status", "flow_id", "status"),
        Index("ix_flow_executions_session", "session_id"),
    )

    flow_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("flows.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
        comment="Flow being executed",
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversation_sessions.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
        comment="Conversation session this execution belongs to",
    )
    current_node_id: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
        comment="ID of the current node being executed",
    )
    collected_data: Mapped[dict] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
        comment="Data collected during flow execution",
    )
    status: Mapped[FlowExecutionStatus] = mapped_column(
        SQLEnum(
            FlowExecutionStatus,
            name="flow_execution_status",
            values_callable=lambda x: [e.value for e in x],
        ),
        default=FlowExecutionStatus.PENDING,
        nullable=False,
        index=True,
        comment="Current execution status",
    )
    result_data: Mapped[dict | None] = mapped_column(
        JSON,
        nullable=True,
        comment="Final execution results",
    )
    error_message: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Error details if execution failed",
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When the execution started",
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When the execution completed or failed",
    )

    # Relationships
    flow: Mapped["Flow"] = relationship(
        "Flow",
        back_populates="executions",
    )

    def __repr__(self) -> str:
        return f"<FlowExecution(id={self.id}, flow_id={self.flow_id}, status={self.status})>"


class FlowTemplate(Base, UUIDMixin, TimestampMixin):
    """
    Pre-built flow templates for common use cases.

    Provides reusable flow patterns that tenants can instantiate
    and customize for their specific needs.
    """

    __tablename__ = "flow_templates"

    name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        unique=True,
        comment="Template name",
    )
    description: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="What this template does",
    )
    category: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        index=True,
        comment="Template category: support, sales, onboarding, etc.",
    )
    template_nodes: Mapped[dict] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
        comment="Template flow structure: {nodes: [], edges: [], start_node_id}",
    )
    trigger_config: Mapped[dict] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
        comment="Default trigger configuration for this template",
    )
    min_tier: Mapped[str] = mapped_column(
        String(50),
        default="trial",
        nullable=False,
        comment="Minimum subscription tier required: trial, lite, professional, enterprise",
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
        index=True,
        comment="Whether this template is available for use",
    )

    def __repr__(self) -> str:
        return f"<FlowTemplate(id={self.id}, name={self.name}, category={self.category})>"
