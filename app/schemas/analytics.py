"""Analytics Pydantic schemas."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


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
    conversations_by_status: dict[str, int] = Field(..., description="Count by status")
    escalation_rate: float
    avg_session_duration_minutes: float
    avg_messages_per_session: float
    top_escalation_reasons: list[dict[str, Any]] = Field(
        default_factory=list, description="Most common escalation reasons"
    )


class TicketMetricsResponse(BaseModel):
    """Detailed ticket analytics response."""

    model_config = ConfigDict(from_attributes=True)

    tenant_id: UUID
    period_start: datetime
    period_end: datetime

    total_tickets: int
    tickets_by_status: dict[str, int]
    tickets_by_priority: dict[str, int]
    avg_resolution_time_hours: float
    resolution_time_by_priority: dict[str, float] = Field(
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
    ratings_count: int = Field(0, description="Ratings received in the window (rating created_at)")
    average_rating: float | None = Field(None, description="Average score in the window (2 dp)")
    csat_percent: float | None = Field(
        None, description="% of window ratings scoring >= 4 (1 dp); null when none"
    )


class AgentSummaryResponse(BaseModel):
    """Agent summary for list view."""

    model_config = ConfigDict(from_attributes=True)

    agent_id: UUID
    agent_name: str
    email: str
    current_assigned_tickets: int
    tickets_by_status: dict[str, int]
    total_tickets_resolved: int = Field(..., description="All-time resolved tickets")
    ratings_count: int = Field(0, description="Ratings received in the window (rating created_at)")
    average_rating: float | None = Field(None, description="Average score in the window (2 dp)")
    csat_percent: float | None = Field(
        None, description="% of window ratings scoring >= 4 (1 dp); null when none"
    )


class TrendDataPoint(BaseModel):
    """Single data point in a time-series trend."""

    model_config = ConfigDict(from_attributes=True)

    timestamp: datetime
    value: float
    meta: dict[str, Any] | None = None


class TrendResponse(BaseModel):
    """Time-series trend data response."""

    model_config = ConfigDict(from_attributes=True)

    tenant_id: UUID
    metric_name: str
    granularity: str = Field(..., description="daily, weekly, or monthly")
    period_start: datetime
    period_end: datetime
    data_points: list[TrendDataPoint]


class KnowledgeUsageResponse(BaseModel):
    """Knowledge base usage and effectiveness metrics."""

    model_config = ConfigDict(from_attributes=True)

    tenant_id: UUID
    period_start: datetime
    period_end: datetime

    total_knowledge_chunks: int
    chunks_used_in_conversations: int
    usage_rate: float = Field(..., description="Percentage of chunks used")
    top_used_chunks: list[dict[str, Any]] = Field(
        default_factory=list, description="Most frequently used knowledge chunks"
    )
    avg_chunks_per_conversation: float
    knowledge_effectiveness_score: float = Field(
        ..., description="Ratio of resolved to escalated conversations using knowledge"
    )


class VisitTrackRequest(BaseModel):
    """Request payload for tracking a dashboard visit."""

    source: Literal["dashboard"] = Field(
        default="dashboard",
        description="Visit source for authenticated tenant users",
    )
    page_path: str | None = Field(
        default=None,
        max_length=255,
        description="Optional dashboard path where visit was recorded",
    )
