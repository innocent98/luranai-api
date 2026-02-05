"""Flow and workflow automation Pydantic schemas."""

from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field, ConfigDict

from app.db.models.flow import (
    FlowStatus,
    FlowTriggerType,
    FlowExecutionStatus,
)


# Base schemas
class FlowBase(BaseModel):
    """Base flow schema."""

    name: str = Field(..., min_length=1, max_length=255, description="Human-readable flow name")
    description: Optional[str] = Field(None, description="Detailed description of what this flow does")
    trigger_type: FlowTriggerType = Field(..., description="How this flow is triggered")
    trigger_config: dict = Field(
        default_factory=dict,
        description="Trigger configuration: {keywords, intents, schedule, events}",
    )
    nodes: dict = Field(
        default_factory=dict,
        description="Flow node definitions: {nodes: [], edges: [], start_node_id}",
    )
    priority: int = Field(default=0, description="Execution priority (higher = more important)")


class FlowExecutionBase(BaseModel):
    """Base flow execution schema."""

    current_node_id: Optional[str] = Field(None, max_length=100, description="ID of the current node being executed")
    collected_data: dict = Field(default_factory=dict, description="Data collected during flow execution")


class FlowTemplateBase(BaseModel):
    """Base flow template schema."""

    name: str = Field(..., min_length=1, max_length=255, description="Template name")
    description: Optional[str] = Field(None, description="What this template does")
    category: str = Field(..., min_length=1, max_length=100, description="Template category: support, sales, onboarding, etc.")
    template_nodes: dict = Field(
        default_factory=dict,
        description="Template flow structure: {nodes: [], edges: [], start_node_id}",
    )
    trigger_config: dict = Field(
        default_factory=dict,
        description="Default trigger configuration for this template",
    )


# Create schemas
class FlowCreate(FlowBase):
    """Schema for creating a new flow."""

    tenant_id: UUID = Field(..., description="Tenant that owns this flow")
    status: FlowStatus = Field(default=FlowStatus.DRAFT, description="Initial flow status")
    is_active: bool = Field(default=True, description="Whether this flow can be triggered")
    version: int = Field(default=1, ge=1, description="Flow version number")
    created_by: Optional[UUID] = Field(None, description="User who created this flow")


class FlowExecutionCreate(BaseModel):
    """Schema for creating a flow execution."""

    flow_id: UUID = Field(..., description="Flow being executed")
    session_id: UUID = Field(..., description="Conversation session this execution belongs to")
    current_node_id: Optional[str] = Field(None, max_length=100, description="ID of the current node being executed")
    collected_data: dict = Field(default_factory=dict, description="Data collected during flow execution")
    status: FlowExecutionStatus = Field(default=FlowExecutionStatus.PENDING, description="Initial execution status")


class FlowTemplateCreate(FlowTemplateBase):
    """Schema for creating a flow template."""

    min_tier: str = Field(
        default="trial",
        max_length=50,
        description="Minimum subscription tier required: trial, lite, professional, enterprise",
    )
    is_active: bool = Field(default=True, description="Whether this template is available for use")


# Update schemas
class FlowUpdate(BaseModel):
    """Schema for updating a flow."""

    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None
    status: Optional[FlowStatus] = None
    is_active: Optional[bool] = None
    trigger_type: Optional[FlowTriggerType] = None
    trigger_config: Optional[dict] = None
    nodes: Optional[dict] = None
    priority: Optional[int] = None
    version: Optional[int] = Field(None, ge=1)


class FlowExecutionUpdate(BaseModel):
    """Schema for updating a flow execution."""

    model_config = ConfigDict(extra="forbid")

    current_node_id: Optional[str] = Field(None, max_length=100)
    collected_data: Optional[dict] = None
    status: Optional[FlowExecutionStatus] = None
    result_data: Optional[dict] = None
    error_message: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


class FlowTemplateUpdate(BaseModel):
    """Schema for updating a flow template."""

    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None
    category: Optional[str] = Field(None, min_length=1, max_length=100)
    template_nodes: Optional[dict] = None
    trigger_config: Optional[dict] = None
    min_tier: Optional[str] = Field(None, max_length=50)
    is_active: Optional[bool] = None


# Response schemas
class FlowResponse(FlowBase):
    """Schema for flow response."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    status: FlowStatus
    is_active: bool
    version: int
    created_by: Optional[UUID] = None
    created_at: datetime
    updated_at: datetime


class FlowDetailResponse(FlowResponse):
    """Schema for flow with execution history."""

    execution_count: int = Field(default=0, description="Total number of executions")
    last_executed_at: Optional[datetime] = Field(None, description="When this flow was last executed")


class FlowExecutionResponse(BaseModel):
    """Schema for flow execution response."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    flow_id: UUID
    session_id: UUID
    current_node_id: Optional[str] = None
    collected_data: dict
    status: FlowExecutionStatus
    result_data: Optional[dict] = None
    error_message: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class FlowExecutionDetailResponse(FlowExecutionResponse):
    """Schema for flow execution with flow details."""

    flow_name: str = Field(..., description="Name of the executed flow")
    flow_description: Optional[str] = Field(None, description="Description of the executed flow")


class FlowTemplateResponse(FlowTemplateBase):
    """Schema for flow template response."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    min_tier: str
    is_active: bool
    created_at: datetime
    updated_at: datetime


class FlowListResponse(BaseModel):
    """Schema for flow list response."""

    flows: list[FlowResponse]
    total: int
    page: int
    page_size: int


class FlowExecutionListResponse(BaseModel):
    """Schema for flow execution list response."""

    executions: list[FlowExecutionResponse]
    total: int
    page: int
    page_size: int


class FlowTemplateListResponse(BaseModel):
    """Schema for flow template list response."""

    templates: list[FlowTemplateResponse]
    total: int
    page: int
    page_size: int


# Request schemas
class FlowInstantiateRequest(BaseModel):
    """Schema for instantiating a flow from a template."""

    template_id: UUID = Field(..., description="Flow template to instantiate")
    tenant_id: UUID = Field(..., description="Tenant that will own the new flow")
    name: str = Field(..., min_length=1, max_length=255, description="Name for the new flow")
    description: Optional[str] = Field(None, description="Custom description for the new flow")
    customize_trigger: Optional[dict] = Field(None, description="Custom trigger configuration")
    customize_nodes: Optional[dict] = Field(None, description="Custom node configuration")
    created_by: Optional[UUID] = Field(None, description="User creating the flow")


class FlowExecuteRequest(BaseModel):
    """Schema for manually executing a flow."""

    flow_id: UUID = Field(..., description="Flow to execute")
    session_id: UUID = Field(..., description="Conversation session to execute in")
    initial_data: dict = Field(default_factory=dict, description="Initial data for the execution")


class FlowValidationResponse(BaseModel):
    """Schema for flow validation response."""

    is_valid: bool = Field(..., description="Whether the flow configuration is valid")
    errors: list[str] = Field(default_factory=list, description="Validation error messages")
    warnings: list[str] = Field(default_factory=list, description="Validation warnings")
