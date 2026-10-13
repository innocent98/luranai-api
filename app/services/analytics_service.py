"""Analytics service for calculating and retrieving analytics metrics."""

from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import extract, func
from sqlalchemy.orm import Session

from app.db.models.conversation import (
    ConversationSession,
    Message,
    SessionStatus,
    Ticket,
    TicketPriority,
    TicketStatus,
)
from app.db.models.knowledge import KnowledgeChunk
from app.db.models.user import User
from app.schemas.analytics import (
    AgentPerformanceResponse,
    AgentSummaryResponse,
    AnalyticsOverviewResponse,
    ConversationMetricsResponse,
    KnowledgeUsageResponse,
    TicketMetricsResponse,
    TrendDataPoint,
    TrendResponse,
)
from app.services.rating_read_service import (
    NO_RATINGS,
    agent_rating_stats,
    per_agent_rating_stats,
)
from app.utils.datetime import utcnow_naive


class AnalyticsService:
    """Service for calculating and retrieving analytics metrics."""

    def __init__(self, db: Session):
        """Initialize analytics service with database session."""
        self.db = db

    def _get_default_date_range(self) -> tuple[datetime, datetime]:
        """
        Get default date range (last 30 days).

        Returns:
            Tuple of (start_date, end_date)
        """
        end_date = utcnow_naive()
        start_date = end_date - timedelta(days=30)
        return start_date, end_date

    def get_overview_metrics(
        self,
        tenant_id: UUID,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
    ) -> AnalyticsOverviewResponse:
        """
        Get overview dashboard metrics for tenant.

        Args:
            tenant_id: Tenant UUID
            start_date: Optional start date (defaults to 30 days ago)
            end_date: Optional end date (defaults to now)

        Returns:
            Overview metrics response
        """
        if not start_date or not end_date:
            start_date, end_date = self._get_default_date_range()

        # Conversation metrics
        conversation_metrics = self._get_conversation_counts(tenant_id, start_date, end_date)
        escalation_rate = self._calculate_escalation_rate(tenant_id, start_date, end_date)

        # Ticket metrics
        ticket_metrics = self._get_ticket_counts(tenant_id, start_date, end_date)
        avg_resolution_time = self._calculate_avg_resolution_time(tenant_id, start_date, end_date)
        unassigned_count = self._get_unassigned_ticket_count(tenant_id)

        # Agent metrics
        total_agents, active_agents = self._get_agent_counts(tenant_id)

        return AnalyticsOverviewResponse(
            tenant_id=tenant_id,
            period_start=start_date,
            period_end=end_date,
            # Conversations
            total_conversations=conversation_metrics.get("total", 0),
            active_conversations=conversation_metrics.get(SessionStatus.ACTIVE.value, 0),
            escalated_conversations=conversation_metrics.get(SessionStatus.ESCALATED.value, 0),
            resolved_conversations=conversation_metrics.get(SessionStatus.RESOLVED.value, 0),
            abandoned_conversations=conversation_metrics.get(SessionStatus.ABANDONED.value, 0),
            escalation_rate=escalation_rate,
            # Tickets
            total_tickets=ticket_metrics.get("total", 0),
            open_tickets=ticket_metrics.get(TicketStatus.OPEN.value, 0),
            in_progress_tickets=ticket_metrics.get(TicketStatus.IN_PROGRESS.value, 0),
            resolved_tickets=ticket_metrics.get(TicketStatus.RESOLVED.value, 0),
            closed_tickets=ticket_metrics.get(TicketStatus.CLOSED.value, 0),
            unassigned_tickets=unassigned_count,
            avg_resolution_time_hours=avg_resolution_time,
            # Agents
            total_agents=total_agents,
            active_agents=active_agents,
        )

    def get_conversation_metrics(
        self,
        tenant_id: UUID,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        status: SessionStatus | None = None,
    ) -> ConversationMetricsResponse:
        """
        Get detailed conversation analytics.

        Args:
            tenant_id: Tenant UUID
            start_date: Optional start date
            end_date: Optional end date
            status: Optional status filter

        Returns:
            Detailed conversation metrics
        """
        if not start_date or not end_date:
            start_date, end_date = self._get_default_date_range()

        # Basic counts
        conversation_counts = self._get_conversation_counts(tenant_id, start_date, end_date)
        escalation_rate = self._calculate_escalation_rate(tenant_id, start_date, end_date)

        # Session duration
        avg_duration = self._calculate_avg_session_duration(tenant_id, start_date, end_date)

        # Messages per session
        avg_messages = self._calculate_avg_messages_per_session(tenant_id, start_date, end_date)

        # Escalation reasons
        top_reasons = self._get_top_escalation_reasons(tenant_id, start_date, end_date)

        # Remove 'total' from status breakdown
        conversations_by_status = {k: v for k, v in conversation_counts.items() if k != "total"}

        return ConversationMetricsResponse(
            tenant_id=tenant_id,
            period_start=start_date,
            period_end=end_date,
            total_conversations=conversation_counts.get("total", 0),
            conversations_by_status=conversations_by_status,
            escalation_rate=escalation_rate,
            avg_session_duration_minutes=avg_duration,
            avg_messages_per_session=avg_messages,
            top_escalation_reasons=top_reasons,
        )

    def get_ticket_metrics(
        self,
        tenant_id: UUID,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        status: TicketStatus | None = None,
        priority: TicketPriority | None = None,
    ) -> TicketMetricsResponse:
        """
        Get detailed ticket analytics.

        Args:
            tenant_id: Tenant UUID
            start_date: Optional start date
            end_date: Optional end date
            status: Optional status filter
            priority: Optional priority filter

        Returns:
            Detailed ticket metrics
        """
        if not start_date or not end_date:
            start_date, end_date = self._get_default_date_range()

        # Basic counts
        ticket_counts = self._get_ticket_counts(tenant_id, start_date, end_date)
        tickets_by_priority = self._get_tickets_by_priority(tenant_id, start_date, end_date)

        # Resolution times
        avg_resolution_time = self._calculate_avg_resolution_time(tenant_id, start_date, end_date)
        resolution_by_priority = self._get_resolution_time_by_priority(
            tenant_id, start_date, end_date
        )

        # Unassigned
        unassigned_count = self._get_unassigned_ticket_count(tenant_id)

        # Created vs resolved
        tickets_created = self._get_tickets_created_count(tenant_id, start_date, end_date)
        tickets_resolved = self._get_tickets_resolved_count(tenant_id, start_date, end_date)

        # Remove 'total' from status breakdown
        tickets_by_status = {k: v for k, v in ticket_counts.items() if k != "total"}

        return TicketMetricsResponse(
            tenant_id=tenant_id,
            period_start=start_date,
            period_end=end_date,
            total_tickets=ticket_counts.get("total", 0),
            tickets_by_status=tickets_by_status,
            tickets_by_priority=tickets_by_priority,
            avg_resolution_time_hours=avg_resolution_time,
            resolution_time_by_priority=resolution_by_priority,
            unassigned_tickets=unassigned_count,
            tickets_created=tickets_created,
            tickets_resolved=tickets_resolved,
        )

    def get_agent_performance(
        self,
        tenant_id: UUID,
        agent_id: UUID,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
    ) -> AgentPerformanceResponse:
        """
        Get performance metrics for specific agent.

        Args:
            tenant_id: Tenant UUID
            agent_id: Agent user ID
            start_date: Optional start date
            end_date: Optional end date

        Returns:
            Agent performance metrics
        """
        if not start_date or not end_date:
            start_date, end_date = self._get_default_date_range()

        # Get agent info
        agent = self.db.query(User).filter(User.id == agent_id).first()
        if not agent:
            raise ValueError("Agent not found")

        # Ticket metrics
        total_assigned = (
            self.db.query(func.count(Ticket.id))
            .filter(
                Ticket.tenant_id == tenant_id,
                Ticket.assigned_to == agent_id,
                Ticket.created_at >= start_date,
                Ticket.created_at <= end_date,
            )
            .scalar()
            or 0
        )

        tickets_resolved = (
            self.db.query(func.count(Ticket.id))
            .filter(
                Ticket.tenant_id == tenant_id,
                Ticket.assigned_to == agent_id,
                Ticket.status == TicketStatus.RESOLVED,
                Ticket.resolved_at >= start_date,
                Ticket.resolved_at <= end_date,
            )
            .scalar()
            or 0
        )

        tickets_in_progress = (
            self.db.query(func.count(Ticket.id))
            .filter(
                Ticket.tenant_id == tenant_id,
                Ticket.assigned_to == agent_id,
                Ticket.status == TicketStatus.IN_PROGRESS,
            )
            .scalar()
            or 0
        )

        # Average resolution time for this agent
        avg_resolution = (
            self.db.query(func.avg(extract("epoch", Ticket.resolved_at - Ticket.created_at) / 3600))
            .filter(
                Ticket.tenant_id == tenant_id,
                Ticket.assigned_to == agent_id,
                Ticket.status == TicketStatus.RESOLVED,
                Ticket.resolved_at.isnot(None),
                Ticket.resolved_at >= start_date,
                Ticket.resolved_at <= end_date,
            )
            .scalar()
            or 0.0
        )

        # Conversations handled
        conversations_handled = (
            self.db.query(func.count(ConversationSession.id))
            .filter(
                ConversationSession.tenant_id == tenant_id,
                ConversationSession.agent_id == agent_id,
                ConversationSession.agent_joined_at >= start_date,
                ConversationSession.agent_joined_at <= end_date,
            )
            .scalar()
            or 0
        )

        # Average response time (simplified - time from agent join to first message)
        avg_response_time = 0.0  # Placeholder - would need more complex calculation

        rating = agent_rating_stats(self.db, tenant_id, agent_id, start_date, end_date)

        return AgentPerformanceResponse(
            agent_id=agent_id,
            agent_name=agent.full_name,
            agent_email=agent.email,
            period_start=start_date,
            period_end=end_date,
            total_tickets_assigned=total_assigned,
            tickets_resolved=tickets_resolved,
            tickets_in_progress=tickets_in_progress,
            avg_resolution_time_hours=avg_resolution,
            conversations_handled=conversations_handled,
            avg_response_time_minutes=avg_response_time,
            ratings_count=rating.count,
            average_rating=rating.average,
            csat_percent=rating.csat_percent,
        )

    def get_all_agents_performance(
        self,
        tenant_id: UUID,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
    ) -> list[AgentSummaryResponse]:
        """
        Get performance summary for all agents in tenant.

        Args:
            tenant_id: Tenant UUID
            start_date: Optional start date
            end_date: Optional end date

        Returns:
            List of agent summaries
        """
        if not start_date or not end_date:
            start_date, end_date = self._get_default_date_range()

        ratings = per_agent_rating_stats(self.db, tenant_id, start_date, end_date)

        # Get all agents for this tenant
        agents = self.db.query(User).filter(User.tenant_id == tenant_id).all()

        summaries = []
        for agent in agents:
            # Current assigned tickets
            current_assigned = (
                self.db.query(func.count(Ticket.id))
                .filter(Ticket.assigned_to == agent.id, Ticket.tenant_id == tenant_id)
                .scalar()
                or 0
            )

            # Tickets by status for this agent
            ticket_status_counts = (
                self.db.query(Ticket.status, func.count(Ticket.id))
                .filter(Ticket.assigned_to == agent.id, Ticket.tenant_id == tenant_id)
                .group_by(Ticket.status)
                .all()
            )
            tickets_by_status = {status.value: count for status, count in ticket_status_counts}

            # All-time resolved tickets
            total_resolved = (
                self.db.query(func.count(Ticket.id))
                .filter(
                    Ticket.assigned_to == agent.id,
                    Ticket.tenant_id == tenant_id,
                    Ticket.status == TicketStatus.RESOLVED,
                )
                .scalar()
                or 0
            )

            summaries.append(
                AgentSummaryResponse(
                    agent_id=agent.id,
                    agent_name=agent.full_name,
                    email=agent.email,
                    current_assigned_tickets=current_assigned,
                    tickets_by_status=tickets_by_status,
                    total_tickets_resolved=total_resolved,
                    ratings_count=ratings.get(agent.id, NO_RATINGS).count,
                    average_rating=ratings.get(agent.id, NO_RATINGS).average,
                    csat_percent=ratings.get(agent.id, NO_RATINGS).csat_percent,
                )
            )

        return summaries

    def get_conversation_trends(
        self,
        tenant_id: UUID,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        granularity: str = "daily",
    ) -> TrendResponse:
        """
        Get conversation volume trends over time.

        Args:
            tenant_id: Tenant UUID
            start_date: Optional start date
            end_date: Optional end date
            granularity: Time grouping (daily, weekly, monthly)

        Returns:
            Trend data response
        """
        if not start_date or not end_date:
            start_date, end_date = self._get_default_date_range()

        # Determine date truncation based on granularity
        if granularity == "weekly":
            trunc_func = func.date_trunc("week", ConversationSession.created_at)
        elif granularity == "monthly":
            trunc_func = func.date_trunc("month", ConversationSession.created_at)
        else:  # daily
            trunc_func = func.date_trunc("day", ConversationSession.created_at)

        # Get trend data
        trend_data = (
            self.db.query(
                trunc_func.label("period"),
                func.count(ConversationSession.id).label("count"),
            )
            .filter(
                ConversationSession.tenant_id == tenant_id,
                ConversationSession.created_at >= start_date,
                ConversationSession.created_at <= end_date,
            )
            .group_by("period")
            .order_by("period")
            .all()
        )

        # Convert to data points
        data_points = [
            TrendDataPoint(timestamp=period, value=float(count), meta=None)
            for period, count in trend_data
        ]

        return TrendResponse(
            tenant_id=tenant_id,
            metric_name="conversation_count",
            granularity=granularity,
            period_start=start_date,
            period_end=end_date,
            data_points=data_points,
        )

    def get_ticket_trends(
        self,
        tenant_id: UUID,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        granularity: str = "daily",
    ) -> TrendResponse:
        """
        Get ticket creation/resolution trends over time.

        Args:
            tenant_id: Tenant UUID
            start_date: Optional start date
            end_date: Optional end date
            granularity: Time grouping (daily, weekly, monthly)

        Returns:
            Trend data response
        """
        if not start_date or not end_date:
            start_date, end_date = self._get_default_date_range()

        # Determine date truncation based on granularity
        if granularity == "weekly":
            trunc_func = func.date_trunc("week", Ticket.created_at)
        elif granularity == "monthly":
            trunc_func = func.date_trunc("month", Ticket.created_at)
        else:  # daily
            trunc_func = func.date_trunc("day", Ticket.created_at)

        # Get trend data
        trend_data = (
            self.db.query(trunc_func.label("period"), func.count(Ticket.id).label("count"))
            .filter(
                Ticket.tenant_id == tenant_id,
                Ticket.created_at >= start_date,
                Ticket.created_at <= end_date,
            )
            .group_by("period")
            .order_by("period")
            .all()
        )

        # Convert to data points
        data_points = [
            TrendDataPoint(timestamp=period, value=float(count), meta=None)
            for period, count in trend_data
        ]

        return TrendResponse(
            tenant_id=tenant_id,
            metric_name="ticket_count",
            granularity=granularity,
            period_start=start_date,
            period_end=end_date,
            data_points=data_points,
        )

    def get_knowledge_usage_stats(
        self,
        tenant_id: UUID,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
    ) -> KnowledgeUsageResponse:
        """
        Get knowledge base usage and effectiveness metrics.

        Args:
            tenant_id: Tenant UUID
            start_date: Optional start date
            end_date: Optional end date

        Returns:
            Knowledge usage metrics
        """
        if not start_date or not end_date:
            start_date, end_date = self._get_default_date_range()

        # Total knowledge chunks for tenant
        total_chunks = (
            self.db.query(func.count(KnowledgeChunk.id))
            .filter(KnowledgeChunk.tenant_id == tenant_id)
            .scalar()
            or 0
        )

        # Chunks used in conversations (distinct chunk IDs from messages)
        # This is a simplified version - would need to unnest JSON arrays in production
        chunks_used = 0  # Placeholder
        usage_rate = 0.0 if total_chunks == 0 else (chunks_used / total_chunks) * 100

        # Top used chunks - placeholder
        top_used_chunks: list[dict[str, Any]] = []

        # Average chunks per conversation
        avg_chunks = 0.0  # Placeholder

        # Knowledge effectiveness
        effectiveness_score = 0.0  # Placeholder

        return KnowledgeUsageResponse(
            tenant_id=tenant_id,
            period_start=start_date,
            period_end=end_date,
            total_knowledge_chunks=total_chunks,
            chunks_used_in_conversations=chunks_used,
            usage_rate=usage_rate,
            top_used_chunks=top_used_chunks,
            avg_chunks_per_conversation=avg_chunks,
            knowledge_effectiveness_score=effectiveness_score,
        )

    # Private helper methods

    def _get_conversation_counts(
        self, tenant_id: UUID, start_date: datetime, end_date: datetime
    ) -> dict[str, int]:
        """Get conversation counts by status."""
        counts = (
            self.db.query(
                ConversationSession.status, func.count(ConversationSession.id).label("count")
            )
            .filter(
                ConversationSession.tenant_id == tenant_id,
                ConversationSession.created_at >= start_date,
                ConversationSession.created_at <= end_date,
            )
            .group_by(ConversationSession.status)
            .all()
        )

        result = {status.value: count for status, count in counts}
        result["total"] = sum(result.values())
        return result

    def _calculate_escalation_rate(
        self, tenant_id: UUID, start_date: datetime, end_date: datetime
    ) -> float:
        """Calculate escalation rate as percentage."""
        total = (
            self.db.query(func.count(ConversationSession.id))
            .filter(
                ConversationSession.tenant_id == tenant_id,
                ConversationSession.created_at >= start_date,
                ConversationSession.created_at <= end_date,
            )
            .scalar()
            or 0
        )

        if total == 0:
            return 0.0

        escalated = (
            self.db.query(func.count(ConversationSession.id))
            .filter(
                ConversationSession.tenant_id == tenant_id,
                ConversationSession.status == SessionStatus.ESCALATED,
                ConversationSession.created_at >= start_date,
                ConversationSession.created_at <= end_date,
            )
            .scalar()
            or 0
        )

        return (escalated / total) * 100

    def _get_ticket_counts(
        self, tenant_id: UUID, start_date: datetime, end_date: datetime
    ) -> dict[str, int]:
        """Get ticket counts by status."""
        counts = (
            self.db.query(Ticket.status, func.count(Ticket.id).label("count"))
            .filter(
                Ticket.tenant_id == tenant_id,
                Ticket.created_at >= start_date,
                Ticket.created_at <= end_date,
            )
            .group_by(Ticket.status)
            .all()
        )

        result = {status.value: count for status, count in counts}
        result["total"] = sum(result.values())
        return result

    def _calculate_avg_resolution_time(
        self, tenant_id: UUID, start_date: datetime, end_date: datetime
    ) -> float:
        """Calculate average resolution time in hours."""
        avg_time = (
            self.db.query(func.avg(extract("epoch", Ticket.resolved_at - Ticket.created_at) / 3600))
            .filter(
                Ticket.tenant_id == tenant_id,
                Ticket.status == TicketStatus.RESOLVED,
                Ticket.resolved_at.isnot(None),
                Ticket.created_at >= start_date,
                Ticket.resolved_at <= end_date,
            )
            .scalar()
            or 0.0
        )

        return float(avg_time)

    def _get_unassigned_ticket_count(self, tenant_id: UUID) -> int:
        """Get count of currently unassigned tickets."""
        count = (
            self.db.query(func.count(Ticket.id))
            .filter(
                Ticket.tenant_id == tenant_id,
                Ticket.assigned_to.is_(None),
                Ticket.status.in_([TicketStatus.OPEN, TicketStatus.IN_PROGRESS]),
            )
            .scalar()
            or 0
        )
        return count

    def _get_agent_counts(self, tenant_id: UUID) -> tuple[int, int]:
        """
        Get total agents and active agents (with assignments).

        Returns:
            Tuple of (total_agents, active_agents)
        """
        total_agents = (
            self.db.query(func.count(User.id)).filter(User.tenant_id == tenant_id).scalar() or 0
        )

        active_agents = (
            self.db.query(func.count(func.distinct(Ticket.assigned_to)))
            .filter(
                Ticket.tenant_id == tenant_id,
                Ticket.assigned_to.isnot(None),
                Ticket.status.in_([TicketStatus.OPEN, TicketStatus.IN_PROGRESS]),
            )
            .scalar()
            or 0
        )

        return total_agents, active_agents

    def _calculate_avg_session_duration(
        self, tenant_id: UUID, start_date: datetime, end_date: datetime
    ) -> float:
        """Calculate average session duration in minutes."""
        avg_duration = (
            self.db.query(
                func.avg(
                    extract(
                        "epoch", ConversationSession.updated_at - ConversationSession.created_at
                    )
                    / 60
                )
            )
            .filter(
                ConversationSession.tenant_id == tenant_id,
                ConversationSession.created_at >= start_date,
                ConversationSession.created_at <= end_date,
            )
            .scalar()
            or 0.0
        )

        return float(avg_duration)

    def _calculate_avg_messages_per_session(
        self, tenant_id: UUID, start_date: datetime, end_date: datetime
    ) -> float:
        """Calculate average messages per session."""
        # Get total messages and total sessions
        total_messages = (
            self.db.query(func.count(Message.id))
            .join(ConversationSession, Message.session_id == ConversationSession.id)
            .filter(
                ConversationSession.tenant_id == tenant_id,
                ConversationSession.created_at >= start_date,
                ConversationSession.created_at <= end_date,
            )
            .scalar()
            or 0
        )

        total_sessions = (
            self.db.query(func.count(ConversationSession.id))
            .filter(
                ConversationSession.tenant_id == tenant_id,
                ConversationSession.created_at >= start_date,
                ConversationSession.created_at <= end_date,
            )
            .scalar()
            or 0
        )

        if total_sessions == 0:
            return 0.0

        return total_messages / total_sessions

    def _get_top_escalation_reasons(
        self, tenant_id: UUID, start_date: datetime, end_date: datetime, limit: int = 5
    ) -> list[dict[str, Any]]:
        """Get most common escalation reasons."""
        reasons = (
            self.db.query(
                ConversationSession.escalation_reason,
                func.count(ConversationSession.id).label("count"),
            )
            .filter(
                ConversationSession.tenant_id == tenant_id,
                ConversationSession.status == SessionStatus.ESCALATED,
                ConversationSession.escalation_reason.isnot(None),
                ConversationSession.created_at >= start_date,
                ConversationSession.created_at <= end_date,
            )
            .group_by(ConversationSession.escalation_reason)
            .order_by(func.count(ConversationSession.id).desc())
            .limit(limit)
            .all()
        )

        return [{"reason": reason, "count": count} for reason, count in reasons]

    def _get_tickets_by_priority(
        self, tenant_id: UUID, start_date: datetime, end_date: datetime
    ) -> dict[str, int]:
        """Get ticket counts by priority."""
        counts = (
            self.db.query(Ticket.priority, func.count(Ticket.id).label("count"))
            .filter(
                Ticket.tenant_id == tenant_id,
                Ticket.created_at >= start_date,
                Ticket.created_at <= end_date,
            )
            .group_by(Ticket.priority)
            .all()
        )

        return {priority.value: count for priority, count in counts}

    def _get_resolution_time_by_priority(
        self, tenant_id: UUID, start_date: datetime, end_date: datetime
    ) -> dict[str, float]:
        """Get average resolution time by priority level."""
        resolution_times = (
            self.db.query(
                Ticket.priority,
                func.avg(extract("epoch", Ticket.resolved_at - Ticket.created_at) / 3600).label(
                    "avg_hours"
                ),
            )
            .filter(
                Ticket.tenant_id == tenant_id,
                Ticket.status == TicketStatus.RESOLVED,
                Ticket.resolved_at.isnot(None),
                Ticket.created_at >= start_date,
                Ticket.resolved_at <= end_date,
            )
            .group_by(Ticket.priority)
            .all()
        )

        return {priority.value: float(avg_hours or 0.0) for priority, avg_hours in resolution_times}

    def _get_tickets_created_count(
        self, tenant_id: UUID, start_date: datetime, end_date: datetime
    ) -> int:
        """Get count of tickets created in period."""
        count = (
            self.db.query(func.count(Ticket.id))
            .filter(
                Ticket.tenant_id == tenant_id,
                Ticket.created_at >= start_date,
                Ticket.created_at <= end_date,
            )
            .scalar()
            or 0
        )
        return count

    def _get_tickets_resolved_count(
        self, tenant_id: UUID, start_date: datetime, end_date: datetime
    ) -> int:
        """Get count of tickets resolved in period."""
        count = (
            self.db.query(func.count(Ticket.id))
            .filter(
                Ticket.tenant_id == tenant_id,
                Ticket.status == TicketStatus.RESOLVED,
                Ticket.resolved_at >= start_date,
                Ticket.resolved_at <= end_date,
            )
            .scalar()
            or 0
        )
        return count
