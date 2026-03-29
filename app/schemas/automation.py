"""Automation schemas for tenant actions and action execution audit."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.db.models.automation import (
    ActionAuthType,
    ActionExecutionStatus,
    ActionTriggerSource,
    TenantActionStatus,
    TenantActionType,
)


class TenantActionBase(BaseModel):
    """Base schema for tenant action definitions."""

    name: str = Field(..., min_length=1, max_length=120)
    slug: str = Field(..., min_length=1, max_length=120)
    description: str | None = None
    status: TenantActionStatus = TenantActionStatus.ACTIVE
    type: TenantActionType = TenantActionType.HTTP
    method: str = Field(default="POST", min_length=3, max_length=10)
    url_template: str = Field(..., min_length=1)
    timeout_seconds: int = Field(default=20, ge=1, le=120)
    auth_type: ActionAuthType = ActionAuthType.NONE
    auth_config: dict = Field(default_factory=dict)
    headers_template: dict = Field(default_factory=dict)
    query_template: dict = Field(default_factory=dict)
    body_template: dict = Field(default_factory=dict)
    input_schema: dict = Field(default_factory=dict)
    output_schema: dict = Field(default_factory=dict)
    success_condition: dict | None = None
    error_mapping: dict = Field(default_factory=dict)
    retry_policy: dict = Field(default_factory=lambda: {"max_attempts": 1, "backoff_ms": 0})
    is_sensitive: bool = False
    requires_approval: bool = False
    allow_in_flows: bool = False
    available_in_ai_chat: bool = False


class TenantActionCreate(TenantActionBase):
    """Create schema for tenant actions."""

    pass


class TenantActionUpdate(BaseModel):
    """Update schema for tenant actions."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(None, min_length=1, max_length=120)
    slug: str | None = Field(None, min_length=1, max_length=120)
    description: str | None = None
    status: TenantActionStatus | None = None
    method: str | None = Field(None, min_length=3, max_length=10)
    url_template: str | None = Field(None, min_length=1)
    timeout_seconds: int | None = Field(None, ge=1, le=120)
    auth_type: ActionAuthType | None = None
    auth_config: dict | None = None
    headers_template: dict | None = None
    query_template: dict | None = None
    body_template: dict | None = None
    input_schema: dict | None = None
    output_schema: dict | None = None
    success_condition: dict | None = None
    error_mapping: dict | None = None
    retry_policy: dict | None = None
    is_sensitive: bool | None = None
    requires_approval: bool | None = None
    allow_in_flows: bool | None = None
    available_in_ai_chat: bool | None = None


class TenantActionResponse(TenantActionBase):
    """Response schema for tenant actions."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    created_by: UUID | None = None
    updated_by: UUID | None = None
    created_at: datetime
    updated_at: datetime


class ActionTestRequest(BaseModel):
    """Manual test request for action execution."""

    input_payload: dict = Field(default_factory=dict)


class ActionExecutionResponse(BaseModel):
    """Response schema for action executions."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    session_id: UUID | None = None
    flow_execution_id: UUID | None = None
    flow_id: UUID | None = None
    node_id: str | None = None
    action_id: UUID
    trigger_source: ActionTriggerSource
    status: ActionExecutionStatus
    input_payload: dict
    resolved_request: dict
    response_status_code: int | None = None
    response_headers: dict | None = None
    response_body: dict | None = None
    normalized_output: dict | None = None
    attempt: int
    latency_ms: int | None = None
    error_code: str | None = None
    error_message: str | None = None
    requires_approval: bool
    approval_status: str | None = None
    approved_by: UUID | None = None
    idempotency_key: str | None = None
    meta: dict
    created_at: datetime
    updated_at: datetime


class ActionSecretCreate(BaseModel):
    """Create schema for action secrets."""

    provider: str = Field(..., min_length=1, max_length=50)
    key_name: str = Field(..., min_length=1, max_length=120)
    secret_value: str = Field(..., min_length=1)
    status: str = Field(default="active", min_length=1, max_length=20)


class ActionSecretResponse(BaseModel):
    """Response schema for action secrets (masked)."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    provider: str
    key_name: str
    status: str
    created_at: datetime
    updated_at: datetime
    masked_value: str = "***"


class ActionExecutionApprovalRequest(BaseModel):
    """Approve/reject request for execution records."""

    approved: bool = Field(...)
