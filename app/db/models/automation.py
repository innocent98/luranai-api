"""Automation models for tenant-defined AI actions and execution audit."""

import enum
import uuid

from sqlalchemy import JSON, Boolean, ForeignKey, Index, Integer, String, Text
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDMixin


class TenantActionStatus(str, enum.Enum):
    """Action status enum."""

    ACTIVE = "active"
    INACTIVE = "inactive"
    ARCHIVED = "archived"


class TenantActionType(str, enum.Enum):
    """Action type enum."""

    HTTP = "http"


class ActionAuthType(str, enum.Enum):
    """Supported authentication methods for action execution."""

    NONE = "none"
    BEARER = "bearer"
    API_KEY_HEADER = "api_key_header"


class ActionExecutionStatus(str, enum.Enum):
    """Action execution lifecycle states."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    BLOCKED_POLICY = "blocked_policy"


class ActionTriggerSource(str, enum.Enum):
    """Execution source for auditability."""

    FLOW_NODE = "flow_node"
    MANUAL_TEST = "manual_test"
    AI_CHAT = "ai_chat"


class TenantAction(Base, UUIDMixin, TimestampMixin):
    """Reusable automation action definition for a tenant."""

    __tablename__ = "tenant_actions"
    __table_args__ = (
        Index("ix_tenant_actions_tenant_status", "tenant_id", "status"),
        Index("ix_tenant_actions_tenant_slug", "tenant_id", "slug", unique=True),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    slug: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[TenantActionStatus] = mapped_column(
        SQLEnum(
            TenantActionStatus,
            name="tenant_action_status",
            values_callable=lambda x: [e.value for e in x],
        ),
        default=TenantActionStatus.ACTIVE,
        nullable=False,
        index=True,
    )
    type: Mapped[TenantActionType] = mapped_column(
        SQLEnum(
            TenantActionType,
            name="tenant_action_type",
            values_callable=lambda x: [e.value for e in x],
        ),
        default=TenantActionType.HTTP,
        nullable=False,
    )
    method: Mapped[str] = mapped_column(String(10), default="POST", nullable=False)
    url_template: Mapped[str] = mapped_column(Text, nullable=False)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=20, nullable=False)

    auth_type: Mapped[ActionAuthType] = mapped_column(
        SQLEnum(
            ActionAuthType,
            name="action_auth_type",
            values_callable=lambda x: [e.value for e in x],
        ),
        default=ActionAuthType.NONE,
        nullable=False,
    )
    auth_config: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    headers_template: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    query_template: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    body_template: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    input_schema: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    output_schema: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    success_condition: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error_mapping: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    retry_policy: Mapped[dict] = mapped_column(
        JSON, nullable=False, default=lambda: {"max_attempts": 1, "backoff_ms": 0}
    )

    is_sensitive: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    requires_approval: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    allow_in_flows: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    available_in_ai_chat: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )


class ActionSecret(Base, UUIDMixin, TimestampMixin):
    """Tenant-scoped secret references for automation actions."""

    __tablename__ = "action_secrets"
    __table_args__ = (
        Index("ix_action_secrets_tenant_status", "tenant_id", "status"),
        Index(
            "ix_action_secrets_tenant_provider_key",
            "tenant_id",
            "provider",
            "key_name",
            unique=True,
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    key_name: Mapped[str] = mapped_column(String(120), nullable=False)
    # NOTE: v1 stores plaintext; production hardening should encrypt or vault this.
    secret_value: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")


class TenantAutomationConfig(Base, UUIDMixin, TimestampMixin):
    """Tenant-level automation onboarding/configuration settings."""

    __tablename__ = "tenant_automation_configs"
    __table_args__ = (Index("ix_tenant_automation_configs_tenant_id", "tenant_id", unique=True),)

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    api_base_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    default_runtime_variables: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    lookup_action_mapping: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    identifier_mapping: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    output_mapping: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )


class ActionExecution(Base, UUIDMixin, TimestampMixin):
    """Execution audit trail for action invocations."""

    __tablename__ = "action_executions"
    __table_args__ = (
        Index("ix_action_executions_tenant_created", "tenant_id", "created_at"),
        Index("ix_action_executions_action_created", "action_id", "created_at"),
        Index("ix_action_executions_session_created", "session_id", "created_at"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("conversation_sessions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    flow_execution_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("flow_executions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    flow_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("flows.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    node_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    action_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenant_actions.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )

    trigger_source: Mapped[ActionTriggerSource] = mapped_column(
        SQLEnum(
            ActionTriggerSource,
            name="action_trigger_source",
            values_callable=lambda x: [e.value for e in x],
        ),
        nullable=False,
        default=ActionTriggerSource.FLOW_NODE,
    )
    status: Mapped[ActionExecutionStatus] = mapped_column(
        SQLEnum(
            ActionExecutionStatus,
            name="action_execution_status",
            values_callable=lambda x: [e.value for e in x],
        ),
        nullable=False,
        default=ActionExecutionStatus.QUEUED,
        index=True,
    )

    input_payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    resolved_request: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    response_status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_headers: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    response_body: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    normalized_output: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    attempt: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    requires_approval: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    approval_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    meta: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
