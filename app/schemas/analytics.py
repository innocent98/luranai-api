"""Analytics Pydantic schemas."""

from datetime import datetime
from typing import Optional, Dict, List, Any
from uuid import UUID

from pydantic import BaseModel, Field, ConfigDict


class AnalyticsOverviewResponse(BaseModel):
    """Overview dashboard metrics response."""

    model_config = ConfigDict(from_attributes=True)

    tenant_id: UUID
    period_start: datetime
    period_end: datetime

    # Conversation Metrics
    total_conversations: int
    active_conversations: int
    escalated_conversations: int
    resolved_conversations: int
    abandoned_conversations: int
    escalation_rate: float = Field(..., description="Escalation rate as percentage")
    avg_confidence_score: float

    # Ticket Metrics
    total_tickets: int
    open_tickets: int
    in_progress_tickets: int
    resolved_tickets: int
    closed_tickets: int
    unassigned_tickets: int
    avg_resolution_time_hours: float

    # Agent Metrics
    total_agents: int
    active_agents: int = Field(..., description="Agents with assigned tickets")


class ConversationMetricsResponse(BaseModel):
    """Detailed conversation analytics response."""

    model_config = ConfigDict(from_attributes=True)

    tenant_id: UUID
    period_start: datetime
    period_end: datetime

    total_conversations: int
    conversations_by_status: Dict[str, int] = Field(..., description="Count by status")
    escalation_rate: float
    avg_session_duration_minutes: float
    avg_messages_per_session: float
    avg_confidence_score: float
    confidence_distribution: Dict[str, int] = Field(
        ..., description="Distribution of confidence scores by range"
    )
    top_escalation_reasons: List[Dict[str, Any]] = Field(
        default_factory=list, description="Most common escalation reasons"
    )


class TicketMetricsResponse(BaseModel):
    """Detailed ticket analytics response."""

    model_config = ConfigDict(from_attributes=True)

    tenant_id: UUID
    period_start: datetime
    period_end: datetime

    total_tickets: int
    tickets_by_status: Dict[str, int]
    tickets_by_priority: Dict[str, int]
    avg_resolution_time_hours: float
    resolution_time_by_priority: Dict[str, float] = Field(
        ..., description="Average resolution time by priority level"
    )
    unassigned_tickets: int
    tickets_created: int
    tickets_resolved: int


class AgentPerformanceResponse(BaseModel):
    """Individual agent performance metrics."""

    model_config = ConfigDict(from_attributes=True)

    agent_id: UUID
    agent_name: str
    agent_email: str
    period_start: datetime
    period_end: datetime

    total_tickets_assigned: int
    tickets_resolved: int
    tickets_in_progress: int
    avg_resolution_time_hours: float
    conversations_handled: int
    avg_response_time_minutes: float


class AgentSummaryResponse(BaseModel):
    """Agent summary for list view."""

    model_config = ConfigDict(from_attributes=True)

    agent_id: UUID
    agent_name: str
    email: str
    current_assigned_tickets: int
    tickets_by_status: Dict[str, int]
    total_tickets_resolved: int = Field(..., description="All-time resolved tickets")


class TrendDataPoint(BaseModel):
    """Single data point in a time-series trend."""

    model_config = ConfigDict(from_attributes=True)

    timestamp: datetime
    value: float
    meta: Optional[Dict[str, Any]] = None


class TrendResponse(BaseModel):
    """Time-series trend data response."""

    model_config = ConfigDict(from_attributes=True)

    tenant_id: UUID
    metric_name: str
    granularity: str = Field(..., description="daily, weekly, or monthly")
    period_start: datetime
    period_end: datetime
    data_points: List[TrendDataPoint]


class KnowledgeUsageResponse(BaseModel):
    """Knowledge base usage and effectiveness metrics."""

    model_config = ConfigDict(from_attributes=True)

    tenant_id: UUID
    period_start: datetime
    period_end: datetime

    total_knowledge_chunks: int
    chunks_used_in_conversations: int
    usage_rate: float = Field(..., description="Percentage of chunks used")
    top_used_chunks: List[Dict[str, Any]] = Field(
        default_factory=list, description="Most frequently used knowledge chunks"
    )
    avg_chunks_per_conversation: float
    knowledge_effectiveness_score: float = Field(
        ..., description="Ratio of resolved to escalated conversations using knowledge"
    )
