"""Platform analytics API endpoints for admin dashboards."""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_platform_admin, get_db
from app.schemas.platform_admin import PlatformAdminResponse
from app.services.platform_analytics_service import PlatformAnalyticsService
from app.utils.http_rate_limiter import platform_analytics_rate_limit

router = APIRouter()


@router.get(
    "/revenue",
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(platform_analytics_rate_limit)],
)
def get_revenue_metrics(
    start_date: str | None = Query(None, description="Start date (ISO format)"),
    end_date: str | None = Query(None, description="End date (ISO format)"),
    db: Session = Depends(get_db),
    current_admin: PlatformAdminResponse = Depends(get_current_platform_admin),
):
    """
    Get platform revenue metrics including MRR, ARR, and growth.

    Returns:
        Revenue metrics with MRR, ARR, growth rate, and tier breakdown
    """
    try:
        analytics_service = PlatformAnalyticsService(db)

        # Parse dates if provided
        parsed_start = datetime.fromisoformat(start_date) if start_date else None
        parsed_end = datetime.fromisoformat(end_date) if end_date else None

        metrics = analytics_service.get_revenue_metrics(
            start_date=parsed_start,
            end_date=parsed_end,
        )

        return metrics

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid date format: {str(e)}",
        ) from e
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch revenue metrics: {str(e)}",
        ) from e


@router.get(
    "/subscriptions",
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(platform_analytics_rate_limit)],
)
def get_subscription_metrics(
    days: int = Query(30, ge=1, le=365, description="Number of days to analyze"),
    db: Session = Depends(get_db),
    current_admin: PlatformAdminResponse = Depends(get_current_platform_admin),
):
    """
    Get subscription health metrics.

    Returns:
        Subscription metrics including churn rate, trial conversion, and distribution
    """
    try:
        analytics_service = PlatformAnalyticsService(db)
        metrics = analytics_service.get_subscription_metrics(days=days)

        return metrics

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch subscription metrics: {str(e)}",
        ) from e


@router.get(
    "/growth", status_code=status.HTTP_200_OK, dependencies=[Depends(platform_analytics_rate_limit)]
)
def get_growth_metrics(
    days: int = Query(30, ge=1, le=365, description="Number of days to analyze"),
    db: Session = Depends(get_db),
    current_admin: PlatformAdminResponse = Depends(get_current_platform_admin),
):
    """
    Get growth metrics including signups, upgrades, and trends.

    Returns:
        Growth metrics with signup trends and conversion rates
    """
    try:
        analytics_service = PlatformAnalyticsService(db)
        metrics = analytics_service.get_growth_metrics(days=days)

        return metrics

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch growth metrics: {str(e)}",
        ) from e


@router.get(
    "/usage", status_code=status.HTTP_200_OK, dependencies=[Depends(platform_analytics_rate_limit)]
)
def get_usage_metrics(
    db: Session = Depends(get_db),
    current_admin: PlatformAdminResponse = Depends(get_current_platform_admin),
):
    """
    Get platform-wide usage metrics.

    Returns:
        Usage statistics including conversations, tickets, and knowledge sources
    """
    try:
        analytics_service = PlatformAnalyticsService(db)
        metrics = analytics_service.get_usage_metrics()

        return metrics

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch usage metrics: {str(e)}",
        ) from e


@router.get(
    "/cohorts",
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(platform_analytics_rate_limit)],
)
def get_cohort_analysis(
    cohort_size: str = Query("month", pattern="^(week|month)$", description="Cohort size"),
    months_back: int = Query(6, ge=1, le=24, description="Months to analyze"),
    db: Session = Depends(get_db),
    current_admin: PlatformAdminResponse = Depends(get_current_platform_admin),
):
    """
    Get cohort retention analysis.

    Returns:
        Cohort analysis with retention rates by signup period
    """
    try:
        analytics_service = PlatformAnalyticsService(db)
        metrics = analytics_service.get_cohort_analysis(
            cohort_size=cohort_size,
            months_back=months_back,
        )

        return metrics

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch cohort analysis: {str(e)}",
        ) from e


@router.get(
    "/dashboard",
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(platform_analytics_rate_limit)],
)
def get_dashboard_summary(
    db: Session = Depends(get_db),
    current_admin: PlatformAdminResponse = Depends(get_current_platform_admin),
):
    """
    Get comprehensive dashboard summary with all key metrics.

    Returns:
        Complete dashboard with revenue, subscriptions, growth, and usage metrics
    """
    try:
        analytics_service = PlatformAnalyticsService(db)
        summary = analytics_service.get_dashboard_summary()

        return summary

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch dashboard summary: {str(e)}",
        ) from e


@router.get(
    "/ai-flows",
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(platform_analytics_rate_limit)],
)
def get_ai_flow_comparison(
    days: int = Query(30, ge=7, le=365, description="Number of days to analyze"),
    db: Session = Depends(get_db),
    current_admin: PlatformAdminResponse = Depends(get_current_platform_admin),
):
    """
    Compare AI/RAG and Flow performance across the platform.

    Returns:
        KPI comparison, daily trend, outcomes, and top-tenant mix
    """
    try:
        analytics_service = PlatformAnalyticsService(db)
        metrics = analytics_service.get_ai_flow_comparison_metrics(days=days)
        return metrics
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch AI/Flow comparison metrics: {str(e)}",
        ) from e
