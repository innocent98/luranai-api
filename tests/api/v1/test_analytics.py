"""Integration tests for Analytics API endpoints."""

from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db.models.conversation import (
    ConversationSession,
    Message,
    Ticket,
    SessionStatus,
    TicketStatus,
    TicketPriority,
    MessageRole,
)
from app.db.models.tenant import Tenant, TenantCapabilities, TenantAIConfig
from app.db.models.user import User, UserRole
from app.core.security import get_password_hash, create_access_token


@pytest.fixture
def test_tenant_with_analytics(db: Session):
    """Create a test tenant with analytics enabled."""
    tenant = Tenant(
        id=uuid4(),
        name="analytics_test_tenant",
        display_name="Analytics Test Tenant",
        api_key_hash="test_hash",
    )
    db.add(tenant)

    ai_config = TenantAIConfig(
        id=uuid4(),
        tenant_id=tenant.id,
        brand_voice={},
        escalation_keywords=[],
    )
    db.add(ai_config)

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
def test_tenant_without_analytics(db: Session):
    """Create a test tenant without analytics enabled."""
    tenant = Tenant(
        id=uuid4(),
        name="no_analytics_tenant",
        display_name="No Analytics Tenant",
        api_key_hash="test_hash",
    )
    db.add(tenant)

    ai_config = TenantAIConfig(
        id=uuid4(),
        tenant_id=tenant.id,
        brand_voice={},
        escalation_keywords=[],
    )
    db.add(ai_config)

    capabilities = TenantCapabilities(
        id=uuid4(),
        tenant_id=tenant.id,
        can_access_analytics=False,  # Analytics disabled
        can_create_tickets=True,
        can_upload_knowledge=True,
    )
    db.add(capabilities)
    db.commit()
    db.refresh(tenant)
    return tenant


@pytest.fixture
def test_admin_user(db: Session, test_tenant_with_analytics: Tenant):
    """Create a test admin user."""
    user = User(
        id=uuid4(),
        tenant_id=test_tenant_with_analytics.id,
        email="admin@test.com",
        full_name="Test Admin",
        role=UserRole.TENANT_ADMIN,
        hashed_password=get_password_hash("password123"),
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@pytest.fixture
def test_agent_user(db: Session, test_tenant_with_analytics: Tenant):
    """Create a test support agent user."""
    user = User(
        id=uuid4(),
        tenant_id=test_tenant_with_analytics.id,
        email="agent@test.com",
        full_name="Test Agent",
        role=UserRole.SUPPORT_AGENT,
        hashed_password=get_password_hash("password123"),
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@pytest.fixture
def admin_auth_headers(db: Session, test_admin_user: User):
    """Get authentication headers for admin user."""
    token = create_access_token(
        subject=str(test_admin_user.id),
        additional_claims={
            "email": test_admin_user.email,
            "role": test_admin_user.role.value,
            "tenant_id": str(test_admin_user.tenant_id),
        }
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def agent_auth_headers(db: Session, test_agent_user: User):
    """Get authentication headers for agent user."""
    token = create_access_token(
        subject=str(test_agent_user.id),
        additional_claims={
            "email": test_agent_user.email,
            "role": test_agent_user.role.value,
            "tenant_id": str(test_agent_user.tenant_id),
        }
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def sample_analytics_data(db: Session, test_tenant_with_analytics: Tenant, test_agent_user: User):
    """Create sample data for analytics testing."""
    now = datetime.utcnow()

    # Create conversations
    sessions = []
    for i in range(5):
        session = ConversationSession(
            id=uuid4(),
            tenant_id=test_tenant_with_analytics.id,
            user_identifier=f"user_{i}",
            status=SessionStatus.ACTIVE if i < 2 else SessionStatus.ESCALATED,
            escalation_reason="Test reason" if i >= 2 else None,
            agent_id=test_agent_user.id if i >= 2 else None,
            created_at=now - timedelta(days=i),
            last_message_at=now - timedelta(hours=i),
        )
        db.add(session)
        sessions.append(session)

        # Add messages
        for j in range(2):
            message = Message(
                id=uuid4(),
                session_id=session.id,
                role=MessageRole.ASSISTANT if j % 2 == 0 else MessageRole.USER,
                content=f"Test message {j}",
                created_at=now - timedelta(days=i, hours=j),
            )
            db.add(message)

    # Create tickets
    for i, session in enumerate(sessions[2:]):  # Only for escalated sessions
        ticket = Ticket(
            id=uuid4(),
            tenant_id=test_tenant_with_analytics.id,
            session_id=session.id,
            ticket_number=f"TKT-2026-{i:04d}",
            status=TicketStatus.OPEN if i == 0 else TicketStatus.IN_PROGRESS,
            priority=TicketPriority.HIGH if i % 2 == 0 else TicketPriority.MEDIUM,
            summary=f"Test ticket {i}",
            assigned_to=test_agent_user.id,
            created_at=now - timedelta(days=i),
        )
        db.add(ticket)

    db.commit()
    return sessions


class TestAnalyticsEndpoints:
    """Test suite for Analytics API endpoints."""

    def test_get_overview_success(
        self, client: TestClient, admin_auth_headers: dict, sample_analytics_data
    ):
        """Test successful overview metrics retrieval."""
        response = client.get("/api/v1/analytics/overview", headers=admin_auth_headers)

        assert response.status_code == 200
        data = response.json()

        assert "tenant_id" in data
        assert "total_conversations" in data
        assert "total_tickets" in data
        assert "escalation_rate" in data
        assert data["total_conversations"] == 5
        assert data["total_tickets"] == 3

    def test_get_overview_with_date_range(
        self, client: TestClient, admin_auth_headers: dict, sample_analytics_data
    ):
        """Test overview metrics with custom date range."""
        start_date = (datetime.utcnow() - timedelta(days=7)).isoformat()
        end_date = datetime.utcnow().isoformat()

        response = client.get(
            f"/api/v1/analytics/overview?start_date={start_date}&end_date={end_date}",
            headers=admin_auth_headers,
        )

        assert response.status_code == 200
        data = response.json()
        assert "period_start" in data
        assert "period_end" in data

    def test_get_overview_unauthorized(self, client: TestClient, sample_analytics_data):
        """Test overview metrics without authentication."""
        response = client.get("/api/v1/analytics/overview")

        # Expecting 403 because FastAPI security dependency returns 403 when no credentials provided
        assert response.status_code == 403

    def test_get_overview_analytics_disabled(
        self, db: Session, client: TestClient, test_tenant_without_analytics: Tenant
    ):
        """Test overview metrics when analytics is disabled for tenant."""
        # Create user for tenant without analytics
        user = User(
            id=uuid4(),
            tenant_id=test_tenant_without_analytics.id,
            email="user@noanalytics.com",
            full_name="No Analytics User",
            role=UserRole.TENANT_ADMIN,
            hashed_password=get_password_hash("password123"),
            is_active=True,
        )
        db.add(user)
        db.commit()

        token = create_access_token(
            subject=str(user.id),
            additional_claims={
                "email": user.email,
                "role": user.role.value,
                "tenant_id": str(user.tenant_id),
            }
        )
        headers = {"Authorization": f"Bearer {token}"}

        response = client.get("/api/v1/analytics/overview", headers=headers)

        assert response.status_code == 403
        assert "Analytics access not enabled" in response.json()["detail"]

    def test_get_conversation_analytics(
        self, client: TestClient, admin_auth_headers: dict, sample_analytics_data
    ):
        """Test detailed conversation analytics."""
        response = client.get("/api/v1/analytics/conversations", headers=admin_auth_headers)

        assert response.status_code == 200
        data = response.json()

        assert "total_conversations" in data
        assert "conversations_by_status" in data
        assert "escalation_rate" in data
        assert "avg_messages_per_session" in data

    def test_get_conversation_analytics_with_filter(
        self, client: TestClient, admin_auth_headers: dict, sample_analytics_data
    ):
        """Test conversation analytics with status filter."""
        response = client.get(
            f"/api/v1/analytics/conversations?status_filter={SessionStatus.ACTIVE.value}",
            headers=admin_auth_headers,
        )

        assert response.status_code == 200

    def test_get_ticket_analytics(
        self, client: TestClient, admin_auth_headers: dict, sample_analytics_data
    ):
        """Test detailed ticket analytics."""
        response = client.get("/api/v1/analytics/tickets", headers=admin_auth_headers)

        assert response.status_code == 200
        data = response.json()

        assert "total_tickets" in data
        assert "tickets_by_status" in data
        assert "tickets_by_priority" in data
        assert "avg_resolution_time_hours" in data

    def test_get_ticket_analytics_with_filters(
        self, client: TestClient, admin_auth_headers: dict, sample_analytics_data
    ):
        """Test ticket analytics with status and priority filters."""
        response = client.get(
            f"/api/v1/analytics/tickets?status_filter={TicketStatus.OPEN.value}&priority_filter={TicketPriority.HIGH.value}",
            headers=admin_auth_headers,
        )

        assert response.status_code == 200

    def test_get_all_agents_performance_admin_only(
        self, client: TestClient, admin_auth_headers: dict, agent_auth_headers: dict, sample_analytics_data
    ):
        """Test that only admins can access all agents performance."""
        # Admin should succeed
        response = client.get("/api/v1/analytics/agents", headers=admin_auth_headers)
        assert response.status_code == 200

        # Regular agent should fail
        response = client.get("/api/v1/analytics/agents", headers=agent_auth_headers)
        assert response.status_code == 403

    def test_get_all_agents_performance(
        self, client: TestClient, admin_auth_headers: dict, sample_analytics_data
    ):
        """Test all agents performance summary."""
        response = client.get("/api/v1/analytics/agents", headers=admin_auth_headers)

        assert response.status_code == 200
        data = response.json()

        assert isinstance(data, list)
        if len(data) > 0:
            assert "agent_id" in data[0]
            assert "agent_name" in data[0]
            assert "email" in data[0]
            assert "current_assigned_tickets" in data[0]

    def test_get_agent_performance_own_metrics(
        self, client: TestClient, agent_auth_headers: dict, test_agent_user: User, sample_analytics_data
    ):
        """Test that agents can view their own performance metrics."""
        response = client.get(
            f"/api/v1/analytics/agents/{test_agent_user.id}", headers=agent_auth_headers
        )

        assert response.status_code == 200
        data = response.json()

        assert data["agent_id"] == str(test_agent_user.id)
        assert data["agent_name"] == test_agent_user.full_name

    def test_get_agent_performance_other_agent_forbidden(
        self, client: TestClient, agent_auth_headers: dict, sample_analytics_data
    ):
        """Test that agents cannot view other agents' metrics."""
        other_agent_id = uuid4()

        response = client.get(
            f"/api/v1/analytics/agents/{other_agent_id}", headers=agent_auth_headers
        )

        # Should fail because agent can only view own metrics
        assert response.status_code == 403

    def test_get_agent_performance_admin_can_view_any(
        self, client: TestClient, admin_auth_headers: dict, test_agent_user: User, sample_analytics_data
    ):
        """Test that admins can view any agent's metrics."""
        response = client.get(
            f"/api/v1/analytics/agents/{test_agent_user.id}", headers=admin_auth_headers
        )

        assert response.status_code == 200

    def test_get_agent_performance_not_found(
        self, client: TestClient, admin_auth_headers: dict, sample_analytics_data
    ):
        """Test agent performance with non-existent agent."""
        fake_agent_id = uuid4()

        response = client.get(
            f"/api/v1/analytics/agents/{fake_agent_id}", headers=admin_auth_headers
        )

        assert response.status_code == 404

    @pytest.mark.skip(reason="date_trunc not supported in SQLite (PostgreSQL-specific)")
    def test_get_conversation_trends(
        self, client: TestClient, admin_auth_headers: dict, sample_analytics_data
    ):
        """Test conversation trends endpoint."""
        response = client.get(
            "/api/v1/analytics/trends/conversations?granularity=daily",
            headers=admin_auth_headers,
        )

        assert response.status_code == 200
        data = response.json()

        assert "metric_name" in data
        assert data["metric_name"] == "conversation_count"
        assert "granularity" in data
        assert data["granularity"] == "daily"
        assert "data_points" in data

    @pytest.mark.skip(reason="date_trunc not supported in SQLite (PostgreSQL-specific)")
    def test_get_conversation_trends_weekly(
        self, client: TestClient, admin_auth_headers: dict, sample_analytics_data
    ):
        """Test conversation trends with weekly granularity."""
        response = client.get(
            "/api/v1/analytics/trends/conversations?granularity=weekly",
            headers=admin_auth_headers,
        )

        assert response.status_code == 200
        data = response.json()
        assert data["granularity"] == "weekly"

    def test_get_conversation_trends_invalid_granularity(
        self, client: TestClient, admin_auth_headers: dict, sample_analytics_data
    ):
        """Test conversation trends with invalid granularity."""
        response = client.get(
            "/api/v1/analytics/trends/conversations?granularity=invalid",
            headers=admin_auth_headers,
        )

        assert response.status_code == 422  # Validation error

    @pytest.mark.skip(reason="date_trunc not supported in SQLite (PostgreSQL-specific)")
    def test_get_ticket_trends(
        self, client: TestClient, admin_auth_headers: dict, sample_analytics_data
    ):
        """Test ticket trends endpoint."""
        response = client.get(
            "/api/v1/analytics/trends/tickets?granularity=daily", headers=admin_auth_headers
        )

        assert response.status_code == 200
        data = response.json()

        assert data["metric_name"] == "ticket_count"
        assert "data_points" in data

    def test_get_knowledge_analytics(
        self, client: TestClient, admin_auth_headers: dict, sample_analytics_data
    ):
        """Test knowledge usage analytics."""
        response = client.get("/api/v1/analytics/knowledge", headers=admin_auth_headers)

        assert response.status_code == 200
        data = response.json()

        assert "total_knowledge_chunks" in data
        assert "chunks_used_in_conversations" in data
        assert "usage_rate" in data
        assert "knowledge_effectiveness_score" in data

    def test_invalid_date_range(
        self, client: TestClient, admin_auth_headers: dict, sample_analytics_data
    ):
        """Test analytics with invalid date range (end before start)."""
        start_date = datetime.utcnow().isoformat()
        end_date = (datetime.utcnow() - timedelta(days=7)).isoformat()

        response = client.get(
            f"/api/v1/analytics/overview?start_date={start_date}&end_date={end_date}",
            headers=admin_auth_headers,
        )

        assert response.status_code == 400
        assert "End date must be after start date" in response.json()["detail"]

    def test_tenant_isolation(
        self, db: Session, client: TestClient, admin_auth_headers: dict, sample_analytics_data
    ):
        """Test that users can only access their own tenant's analytics."""
        # Create a second tenant
        other_tenant = Tenant(
            id=uuid4(),
            name="other_tenant",
            display_name="Other Tenant",
            api_key_hash="test_hash",
        )
        db.add(other_tenant)

        other_capabilities = TenantCapabilities(
            id=uuid4(),
            tenant_id=other_tenant.id,
            can_access_analytics=True,
        )
        db.add(other_capabilities)
        db.commit()

        # The admin user from test_tenant_with_analytics should only see their own data
        response = client.get("/api/v1/analytics/overview", headers=admin_auth_headers)

        assert response.status_code == 200
        data = response.json()

        # Should only see conversations from their own tenant
        assert data["total_conversations"] == 5  # From sample_analytics_data

    def test_empty_analytics_data(
        self, db: Session, client: TestClient, test_tenant_with_analytics: Tenant
    ):
        """Test analytics endpoints with no data."""
        # Create a new user with no conversations/tickets
        empty_user = User(
            id=uuid4(),
            tenant_id=test_tenant_with_analytics.id,
            email="empty@test.com",
            full_name="Empty User",
            role=UserRole.TENANT_ADMIN,
            hashed_password=get_password_hash("password123"),
            is_active=True,
        )
        db.add(empty_user)
        db.commit()

        token = create_access_token(
            subject=str(empty_user.id),
            additional_claims={
                "email": empty_user.email,
                "role": empty_user.role.value,
                "tenant_id": str(empty_user.tenant_id),
            }
        )
        headers = {"Authorization": f"Bearer {token}"}

        response = client.get("/api/v1/analytics/overview", headers=headers)

        assert response.status_code == 200
        data = response.json()

        # Should return zero values, not errors
        assert data["total_conversations"] >= 0
        assert data["total_tickets"] >= 0
