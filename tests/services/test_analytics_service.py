"""Unit tests for AnalyticsService."""

from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from app.services.analytics_service import AnalyticsService
from app.db.models.conversation import (
    ConversationSession,
    Message,
    Ticket,
    SessionStatus,
    TicketStatus,
    TicketPriority,
    MessageRole,
)
from app.db.models.tenant import Tenant, TenantCapabilities
from app.db.models.user import User, UserRole


@pytest.fixture
def test_tenant(db: Session):
    """Create a test tenant with analytics enabled."""
    tenant = Tenant(
        id=uuid4(),
        name="test_tenant",
        display_name="Test Tenant",
        api_key_hash="test_hash",
    )
    db.add(tenant)

    capabilities = TenantCapabilities(
        id=uuid4(),
        tenant_id=tenant.id,
        can_access_analytics=True,
        can_create_tickets=True,
        can_upload_knowledge=True,
    )
    db.add(capabilities)
    db.commit()
    db.refresh(tenant)
    return tenant


@pytest.fixture
def test_agent(db: Session, test_tenant: Tenant):
    """Create a test support agent."""
    agent = User(
        id=uuid4(),
        tenant_id=test_tenant.id,
        email="agent@test.com",
        full_name="Test Agent",
        role=UserRole.SUPPORT_AGENT,
        hashed_password="test_hash",
        is_active=True,
        is_verified=True,
    )
    db.add(agent)
    db.commit()
    db.refresh(agent)
    return agent


@pytest.fixture
def test_conversations(db: Session, test_tenant: Tenant, test_agent: User):
    """Create test conversation sessions with various statuses."""
    now = datetime.utcnow()
    sessions = []

    # Create 10 sessions with different statuses
    statuses = [
        SessionStatus.ACTIVE,
        SessionStatus.ACTIVE,
        SessionStatus.ESCALATED,
        SessionStatus.ESCALATED,
        SessionStatus.ESCALATED,
        SessionStatus.RESOLVED,
        SessionStatus.RESOLVED,
        SessionStatus.RESOLVED,
        SessionStatus.RESOLVED,
        SessionStatus.ABANDONED,
    ]

    for i, status in enumerate(statuses):
        session = ConversationSession(
            id=uuid4(),
            tenant_id=test_tenant.id,
            user_identifier=f"user_{i}",
            status=status,
            escalation_reason="Low confidence" if status == SessionStatus.ESCALATED else None,
            escalated_at=now - timedelta(hours=i) if status == SessionStatus.ESCALATED else None,
            agent_id=test_agent.id if status in [SessionStatus.ESCALATED, SessionStatus.RESOLVED] else None,
            agent_joined_at=now - timedelta(hours=i) if status in [SessionStatus.ESCALATED, SessionStatus.RESOLVED] else None,
            confidence_scores=[0.8, 0.7, 0.6] if status == SessionStatus.ACTIVE else [0.3, 0.2],
            created_at=now - timedelta(days=i),
            last_message_at=now - timedelta(hours=i),
        )
        db.add(session)
        sessions.append(session)

        # Add messages to each session
        for j in range(3):
            message = Message(
                id=uuid4(),
                session_id=session.id,
                role=MessageRole.ASSISTANT if j % 2 == 0 else MessageRole.USER,
                content=f"Test message {j}",
                confidence_score=0.75 if j % 2 == 0 else None,
                token_count=100,
                created_at=now - timedelta(days=i, hours=j),
            )
            db.add(message)

    db.commit()
    return sessions


@pytest.fixture
def test_tickets(db: Session, test_tenant: Tenant, test_agent: User, test_conversations):
    """Create test tickets with various statuses."""
    now = datetime.utcnow()
    tickets = []

    # Create tickets for escalated sessions
    escalated_sessions = [s for s in test_conversations if s.status == SessionStatus.ESCALATED]

    priorities = [TicketPriority.HIGH, TicketPriority.MEDIUM, TicketPriority.LOW]
    statuses = [TicketStatus.OPEN, TicketStatus.IN_PROGRESS, TicketStatus.RESOLVED]

    for i, session in enumerate(escalated_sessions):
        ticket = Ticket(
            id=uuid4(),
            tenant_id=test_tenant.id,
            session_id=session.id,
            ticket_number=f"TKT-2026-{i:04d}",
            status=statuses[i % len(statuses)],
            priority=priorities[i % len(priorities)],
            summary=f"Test ticket {i}",
            assigned_to=test_agent.id if i % 2 == 0 else None,
            resolved_at=now - timedelta(hours=i) if statuses[i % len(statuses)] == TicketStatus.RESOLVED else None,
            created_at=now - timedelta(days=i),
        )
        db.add(ticket)
        tickets.append(ticket)

    db.commit()
    return tickets


class TestAnalyticsService:
    """Test suite for AnalyticsService."""

    def test_get_overview_metrics(self, db: Session, test_tenant: Tenant, test_conversations, test_tickets):
        """Test overview metrics calculation."""
        analytics_service = AnalyticsService(db)

        start_date = datetime.utcnow() - timedelta(days=30)
        end_date = datetime.utcnow()

        metrics = analytics_service.get_overview_metrics(test_tenant.id, start_date, end_date)

        assert metrics.tenant_id == test_tenant.id
        assert metrics.total_conversations == 10
        assert metrics.active_conversations == 2
        assert metrics.escalated_conversations == 3
        assert metrics.resolved_conversations == 4
        assert metrics.abandoned_conversations == 1
        assert metrics.escalation_rate == 30.0  # 3 out of 10
        assert metrics.total_tickets == 3
        assert metrics.total_agents > 0

    def test_get_conversation_metrics(self, db: Session, test_tenant: Tenant, test_conversations):
        """Test detailed conversation metrics."""
        analytics_service = AnalyticsService(db)

        start_date = datetime.utcnow() - timedelta(days=30)
        end_date = datetime.utcnow()

        metrics = analytics_service.get_conversation_metrics(test_tenant.id, start_date, end_date)

        assert metrics.tenant_id == test_tenant.id
        assert metrics.total_conversations == 10
        assert metrics.escalation_rate == 30.0
        assert SessionStatus.ACTIVE.value in metrics.conversations_by_status
        assert SessionStatus.ESCALATED.value in metrics.conversations_by_status
        assert metrics.avg_messages_per_session > 0
        assert metrics.avg_confidence_score > 0

    def test_get_ticket_metrics(self, db: Session, test_tenant: Tenant, test_tickets):
        """Test detailed ticket metrics."""
        analytics_service = AnalyticsService(db)

        start_date = datetime.utcnow() - timedelta(days=30)
        end_date = datetime.utcnow()

        metrics = analytics_service.get_ticket_metrics(test_tenant.id, start_date, end_date)

        assert metrics.tenant_id == test_tenant.id
        assert metrics.total_tickets == 3
        assert TicketStatus.OPEN.value in metrics.tickets_by_status
        assert TicketPriority.HIGH.value in metrics.tickets_by_priority
        assert metrics.tickets_created == 3

    def test_get_agent_performance(self, db: Session, test_tenant: Tenant, test_agent: User, test_tickets):
        """Test agent performance metrics."""
        analytics_service = AnalyticsService(db)

        start_date = datetime.utcnow() - timedelta(days=30)
        end_date = datetime.utcnow()

        metrics = analytics_service.get_agent_performance(test_tenant.id, test_agent.id, start_date, end_date)

        assert metrics.agent_id == test_agent.id
        assert metrics.agent_name == test_agent.full_name
        assert metrics.agent_email == test_agent.email
        assert metrics.total_tickets_assigned >= 0
        assert metrics.conversations_handled >= 0

    def test_get_all_agents_performance(self, db: Session, test_tenant: Tenant, test_agent: User):
        """Test all agents performance summary."""
        analytics_service = AnalyticsService(db)

        start_date = datetime.utcnow() - timedelta(days=30)
        end_date = datetime.utcnow()

        summaries = analytics_service.get_all_agents_performance(test_tenant.id, start_date, end_date)

        assert len(summaries) >= 1
        assert any(s.agent_id == test_agent.id for s in summaries)

        agent_summary = next(s for s in summaries if s.agent_id == test_agent.id)
        assert agent_summary.agent_name == test_agent.full_name
        assert agent_summary.email == test_agent.email
        assert agent_summary.current_assigned_tickets >= 0

    def test_get_conversation_trends_daily(self, db: Session, test_tenant: Tenant, test_conversations):
        """Test daily conversation trends."""
        analytics_service = AnalyticsService(db)

        start_date = datetime.utcnow() - timedelta(days=30)
        end_date = datetime.utcnow()

        trends = analytics_service.get_conversation_trends(test_tenant.id, start_date, end_date, "daily")

        assert trends.tenant_id == test_tenant.id
        assert trends.metric_name == "conversation_count"
        assert trends.granularity == "daily"
        assert len(trends.data_points) > 0
        assert all(isinstance(dp.value, float) for dp in trends.data_points)

    def test_get_ticket_trends_weekly(self, db: Session, test_tenant: Tenant, test_tickets):
        """Test weekly ticket trends."""
        analytics_service = AnalyticsService(db)

        start_date = datetime.utcnow() - timedelta(days=30)
        end_date = datetime.utcnow()

        trends = analytics_service.get_ticket_trends(test_tenant.id, start_date, end_date, "weekly")

        assert trends.tenant_id == test_tenant.id
        assert trends.metric_name == "ticket_count"
        assert trends.granularity == "weekly"
        assert len(trends.data_points) >= 0

    def test_escalation_rate_calculation(self, db: Session, test_tenant: Tenant, test_conversations):
        """Test escalation rate calculation."""
        analytics_service = AnalyticsService(db)

        start_date = datetime.utcnow() - timedelta(days=30)
        end_date = datetime.utcnow()

        rate = analytics_service._calculate_escalation_rate(test_tenant.id, start_date, end_date)

        # 3 escalated out of 10 total = 30%
        assert rate == 30.0

    def test_avg_confidence_score(self, db: Session, test_tenant: Tenant, test_conversations):
        """Test average confidence score calculation."""
        analytics_service = AnalyticsService(db)

        start_date = datetime.utcnow() - timedelta(days=30)
        end_date = datetime.utcnow()

        avg_score = analytics_service._calculate_avg_confidence_score(test_tenant.id, start_date, end_date)

        assert avg_score > 0
        assert avg_score <= 1.0

    def test_default_date_range(self, db: Session, test_tenant: Tenant):
        """Test default date range is 30 days."""
        analytics_service = AnalyticsService(db)

        start_date, end_date = analytics_service._get_default_date_range()

        diff = end_date - start_date
        assert diff.days == 30

    def test_agent_not_found(self, db: Session, test_tenant: Tenant):
        """Test agent performance with non-existent agent."""
        analytics_service = AnalyticsService(db)

        fake_agent_id = uuid4()

        with pytest.raises(ValueError, match="Agent not found"):
            analytics_service.get_agent_performance(
                test_tenant.id, fake_agent_id, datetime.utcnow() - timedelta(days=30), datetime.utcnow()
            )

    def test_empty_tenant_metrics(self, db: Session):
        """Test metrics with tenant that has no data."""
        # Create a new tenant with no conversations or tickets
        empty_tenant = Tenant(
            id=uuid4(),
            name="empty_tenant",
            display_name="Empty Tenant",
            api_key_hash="test_hash",
        )
        db.add(empty_tenant)
        db.commit()

        analytics_service = AnalyticsService(db)

        start_date = datetime.utcnow() - timedelta(days=30)
        end_date = datetime.utcnow()

        metrics = analytics_service.get_overview_metrics(empty_tenant.id, start_date, end_date)

        assert metrics.total_conversations == 0
        assert metrics.total_tickets == 0
        assert metrics.escalation_rate == 0.0
        assert metrics.avg_confidence_score == 0.0

    def test_confidence_distribution(self, db: Session, test_tenant: Tenant, test_conversations):
        """Test confidence score distribution calculation."""
        analytics_service = AnalyticsService(db)

        start_date = datetime.utcnow() - timedelta(days=30)
        end_date = datetime.utcnow()

        distribution = analytics_service._get_confidence_distribution(test_tenant.id, start_date, end_date)

        assert isinstance(distribution, dict)
        # Should have at least one range with data
        assert len(distribution) > 0

    def test_top_escalation_reasons(self, db: Session, test_tenant: Tenant, test_conversations):
        """Test top escalation reasons retrieval."""
        analytics_service = AnalyticsService(db)

        start_date = datetime.utcnow() - timedelta(days=30)
        end_date = datetime.utcnow()

        reasons = analytics_service._get_top_escalation_reasons(test_tenant.id, start_date, end_date)

        assert isinstance(reasons, list)
        if len(reasons) > 0:
            assert "reason" in reasons[0]
            assert "count" in reasons[0]
