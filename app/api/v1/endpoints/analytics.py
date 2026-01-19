"""Analytics endpoints."""

from datetime import datetime
from typing import Optional, List
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_current_tenant_admin, get_db, verify_tenant_access
from app.services.analytics_service import AnalyticsService
from app.services.tenant_service import TenantService
from app.schemas.analytics import (
    AnalyticsOverviewResponse,
    ConversationMetricsResponse,
    TicketMetricsResponse,
    AgentPerformanceResponse,
    AgentSummaryResponse,
    TrendResponse,
    KnowledgeUsageResponse,
)
from app.schemas.user import UserResponse
from app.db.models.conversation import SessionStatus, TicketStatus, TicketPriority
from app.db.models.user import UserRole

router = APIRouter()


def check_analytics_permission(db: Session, tenant_id: UUID) -> None:
    """
    Verify tenant has analytics access enabled.

    Args:
        db: Database session
        tenant_id: Tenant UUID

    Raises:
        HTTPException: If analytics access not enabled
    """
    tenant_service = TenantService(db)
    tenant = tenant_service.get_tenant_by_id(tenant_id, include_relations=True)

    if not tenant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found"
        )

    if not tenant.capabilities or not tenant.capabilities.can_access_analytics:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Analytics access not enabled for this tenant",
        )


@router.get("/overview", response_model=AnalyticsOverviewResponse, status_code=status.HTTP_200_OK)
def get_analytics_overview(
    start_date: Optional[datetime] = Query(None, description="Start date for analytics period"),
    end_date: Optional[datetime] = Query(None, description="End date for analytics period"),
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_user),
):
    """
    Get overview dashboard metrics for tenant.

    Defaults to last 30 days if no date range specified.
    Requires user to have can_access_analytics permission.
    Automatically filtered by user's tenant_id.

    Args:
        start_date: Optional start date (defaults to 30 days ago)
        end_date: Optional end date (defaults to now)
        db: Database session
        current_user: Current authenticated user

    Returns:
        Overview analytics metrics
    """
    tenant_id = current_user.tenant_id

    # Check analytics permission
    check_analytics_permission(db, tenant_id)

    # Validate date range
    if start_date and end_date and end_date < start_date:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="End date must be after start date",
        )

    analytics_service = AnalyticsService(db)
    return analytics_service.get_overview_metrics(tenant_id, start_date, end_date)


@router.get(
    "/conversations",
    response_model=ConversationMetricsResponse,
    status_code=status.HTTP_200_OK,
)
def get_conversation_analytics(
    start_date: Optional[datetime] = Query(None, description="Start date for analytics period"),
    end_date: Optional[datetime] = Query(None, description="End date for analytics period"),
    status_filter: Optional[SessionStatus] = Query(None, description="Filter by session status"),
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_user),
):
    """
    Get detailed conversation analytics with optional status filter.

    Args:
        start_date: Optional start date
        end_date: Optional end date
        status_filter: Optional session status filter
        db: Database session
        current_user: Current authenticated user

    Returns:
        Detailed conversation metrics
    """
    tenant_id = current_user.tenant_id

    # Check analytics permission
    check_analytics_permission(db, tenant_id)

    # Validate date range
    if start_date and end_date and end_date < start_date:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="End date must be after start date",
        )

    analytics_service = AnalyticsService(db)
    return analytics_service.get_conversation_metrics(
        tenant_id, start_date, end_date, status_filter
    )


@router.get("/tickets", response_model=TicketMetricsResponse, status_code=status.HTTP_200_OK)
def get_ticket_analytics(
    start_date: Optional[datetime] = Query(None, description="Start date for analytics period"),
    end_date: Optional[datetime] = Query(None, description="End date for analytics period"),
    status_filter: Optional[TicketStatus] = Query(None, description="Filter by ticket status"),
    priority_filter: Optional[TicketPriority] = Query(
        None, description="Filter by ticket priority"
    ),
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_user),
):
    """
    Get detailed ticket analytics with filters.

    Args:
        start_date: Optional start date
        end_date: Optional end date
        status_filter: Optional ticket status filter
        priority_filter: Optional ticket priority filter
        db: Database session
        current_user: Current authenticated user

    Returns:
        Detailed ticket metrics
    """
    tenant_id = current_user.tenant_id

    # Check analytics permission
    check_analytics_permission(db, tenant_id)

    # Validate date range
    if start_date and end_date and end_date < start_date:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="End date must be after start date",
        )

    analytics_service = AnalyticsService(db)
    return analytics_service.get_ticket_metrics(
        tenant_id, start_date, end_date, status_filter, priority_filter
    )


@router.get(
    "/agents", response_model=List[AgentSummaryResponse], status_code=status.HTTP_200_OK
)
def get_all_agents_performance(
    start_date: Optional[datetime] = Query(None, description="Start date for analytics period"),
    end_date: Optional[datetime] = Query(None, description="End date for analytics period"),
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_tenant_admin),
):
    """
    Get performance metrics for all agents in tenant (admin only).

    Args:
        start_date: Optional start date
        end_date: Optional end date
        db: Database session
        current_user: Current tenant admin

    Returns:
        List of agent performance summaries
    """
    tenant_id = current_user.tenant_id

    # Check analytics permission
    check_analytics_permission(db, tenant_id)

    # Validate date range
    if start_date and end_date and end_date < start_date:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="End date must be after start date",
        )

    analytics_service = AnalyticsService(db)
    return analytics_service.get_all_agents_performance(tenant_id, start_date, end_date)


@router.get(
    "/agents/{agent_id}",
    response_model=AgentPerformanceResponse,
    status_code=status.HTTP_200_OK,
)
def get_agent_performance(
    agent_id: UUID,
    start_date: Optional[datetime] = Query(None, description="Start date for analytics period"),
    end_date: Optional[datetime] = Query(None, description="End date for analytics period"),
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_user),
):
    """
    Get performance metrics for specific agent.

    Agents can view their own metrics.
    Admins can view any agent's metrics.

    Args:
        agent_id: Agent user ID
        start_date: Optional start date
        end_date: Optional end date
        db: Database session
        current_user: Current authenticated user

    Returns:
        Agent performance metrics
    """
    tenant_id = current_user.tenant_id

    # Check analytics permission
    check_analytics_permission(db, tenant_id)

    # Verify user can view this agent's metrics
    # Agents can only view their own, admins can view any
    if current_user.role != UserRole.TENANT_ADMIN and current_user.id != agent_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only view your own performance metrics",
        )

    # Validate date range
    if start_date and end_date and end_date < start_date:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="End date must be after start date",
        )

    analytics_service = AnalyticsService(db)
    try:
        return analytics_service.get_agent_performance(tenant_id, agent_id, start_date, end_date)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))


@router.get(
    "/trends/conversations", response_model=TrendResponse, status_code=status.HTTP_200_OK
)
def get_conversation_trends(
    start_date: Optional[datetime] = Query(None, description="Start date for analytics period"),
    end_date: Optional[datetime] = Query(None, description="End date for analytics period"),
    granularity: str = Query(
        "daily",
        regex="^(daily|weekly|monthly)$",
        description="Time granularity (daily, weekly, monthly)",
    ),
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_user),
):
    """
    Get conversation volume trends over time.

    Args:
        start_date: Optional start date
        end_date: Optional end date
        granularity: Time grouping (daily, weekly, monthly)
        db: Database session
        current_user: Current authenticated user

    Returns:
        Conversation trend data
    """
    tenant_id = current_user.tenant_id

    # Check analytics permission
    check_analytics_permission(db, tenant_id)

    # Validate date range
    if start_date and end_date and end_date < start_date:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="End date must be after start date",
        )

    analytics_service = AnalyticsService(db)
    return analytics_service.get_conversation_trends(tenant_id, start_date, end_date, granularity)


@router.get("/trends/tickets", response_model=TrendResponse, status_code=status.HTTP_200_OK)
def get_ticket_trends(
    start_date: Optional[datetime] = Query(None, description="Start date for analytics period"),
    end_date: Optional[datetime] = Query(None, description="End date for analytics period"),
    granularity: str = Query(
        "daily",
        regex="^(daily|weekly|monthly)$",
        description="Time granularity (daily, weekly, monthly)",
    ),
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_user),
):
    """
    Get ticket creation/resolution trends over time.

    Args:
        start_date: Optional start date
        end_date: Optional end date
        granularity: Time grouping (daily, weekly, monthly)
        db: Database session
        current_user: Current authenticated user

    Returns:
        Ticket trend data
    """
    tenant_id = current_user.tenant_id

    # Check analytics permission
    check_analytics_permission(db, tenant_id)

    # Validate date range
    if start_date and end_date and end_date < start_date:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="End date must be after start date",
        )

    analytics_service = AnalyticsService(db)
    return analytics_service.get_ticket_trends(tenant_id, start_date, end_date, granularity)


@router.get("/knowledge", response_model=KnowledgeUsageResponse, status_code=status.HTTP_200_OK)
def get_knowledge_analytics(
    start_date: Optional[datetime] = Query(None, description="Start date for analytics period"),
    end_date: Optional[datetime] = Query(None, description="End date for analytics period"),
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_user),
):
    """
    Get knowledge base usage and effectiveness metrics.

    Args:
        start_date: Optional start date
        end_date: Optional end date
        db: Database session
        current_user: Current authenticated user

    Returns:
        Knowledge usage metrics
    """
    tenant_id = current_user.tenant_id

    # Check analytics permission
    check_analytics_permission(db, tenant_id)

    # Validate date range
    if start_date and end_date and end_date < start_date:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="End date must be after start date",
        )

    analytics_service = AnalyticsService(db)
    return analytics_service.get_knowledge_usage_stats(tenant_id, start_date, end_date)
