"""Platform review and feedback management API endpoints."""

from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.orm import Session

from app.api.deps import get_current_platform_admin, get_db
from app.utils.http_rate_limiter import platform_admin_rate_limit
from app.services.review_service import ReviewService
from app.schemas.platform_admin import PlatformAdminResponse

router = APIRouter()


@router.get("/ratings/all", status_code=status.HTTP_200_OK, dependencies=[Depends(platform_admin_rate_limit)])
def get_all_session_ratings(
    tenant_id: Optional[str] = Query(None, description="Tenant ID filter"),
    min_score: Optional[int] = Query(None, ge=1, le=5, description="Minimum score"),
    max_score: Optional[int] = Query(None, ge=1, le=5, description="Maximum score"),
    days: int = Query(30, ge=1, le=365, description="Days to analyze"),
    limit: int = Query(100, ge=1, le=1000, description="Max results"),
    offset: int = Query(0, ge=0, description="Results offset"),
    db: Session = Depends(get_db),
    current_admin: PlatformAdminResponse = Depends(get_current_platform_admin),
):
    """
    Get all session ratings across tenants.

    Returns:
        Paginated list of session ratings with statistics
    """
    try:
        review_service = ReviewService(db)

        # Parse tenant_id if provided
        parsed_tenant_id = UUID(tenant_id) if tenant_id else None

        results = review_service.get_all_session_ratings(
            tenant_id=parsed_tenant_id,
            min_score=min_score,
            max_score=max_score,
            days=days,
            limit=limit,
            offset=offset,
        )

        return results

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid parameter: {str(e)}",
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch session ratings: {str(e)}",
        )


@router.get("/sentiment-analysis", status_code=status.HTTP_200_OK, dependencies=[Depends(platform_admin_rate_limit)])
def get_sentiment_analysis(
    tenant_id: Optional[str] = Query(None, description="Tenant ID filter"),
    days: int = Query(30, ge=1, le=365, description="Days to analyze"),
    db: Session = Depends(get_db),
    current_admin: PlatformAdminResponse = Depends(get_current_platform_admin),
):
    """
    Get sentiment analysis from ratings and feedback.

    Returns:
        Sentiment analysis with positive/negative breakdown
    """
    try:
        review_service = ReviewService(db)

        # Parse tenant_id if provided
        parsed_tenant_id = UUID(tenant_id) if tenant_id else None

        analysis = review_service.analyze_sentiment(
            tenant_id=parsed_tenant_id,
            days=days,
        )

        return analysis

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid parameter: {str(e)}",
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to analyze sentiment: {str(e)}",
        )


@router.get("/tenant-health-scores", status_code=status.HTTP_200_OK, dependencies=[Depends(platform_admin_rate_limit)])
def get_tenant_health_scores(
    days: int = Query(30, ge=1, le=365, description="Days to analyze"),
    limit: int = Query(50, ge=1, le=200, description="Max tenants to return"),
    db: Session = Depends(get_db),
    current_admin: PlatformAdminResponse = Depends(get_current_platform_admin),
):
    """
    Calculate health scores for tenants based on feedback.

    Returns:
        List of tenants with health scores and categories
    """
    try:
        review_service = ReviewService(db)

        scores = review_service.calculate_tenant_health_scores(
            days=days,
            limit=limit,
        )

        return scores

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to calculate tenant health scores: {str(e)}",
        )


@router.get("/feedback/platform", status_code=status.HTTP_200_OK, dependencies=[Depends(platform_admin_rate_limit)])
def get_platform_feedback_all(
    tenant_id: Optional[str] = Query(None, description="Tenant ID filter"),
    category: Optional[str] = Query(None, description="Feedback category filter"),
    status: Optional[str] = Query(None, description="Status filter"),
    days: int = Query(30, ge=1, le=365, description="Days to analyze"),
    limit: int = Query(100, ge=1, le=1000, description="Max results"),
    offset: int = Query(0, ge=0, description="Results offset"),
    db: Session = Depends(get_db),
    current_admin: PlatformAdminResponse = Depends(get_current_platform_admin),
):
    """
    Get platform feedback across all tenants.

    Returns:
        Paginated list of platform feedback
    """
    try:
        from datetime import datetime, timedelta

        review_service = ReviewService(db)

        # Parse tenant_id if provided
        parsed_tenant_id = UUID(tenant_id) if tenant_id else None

        # Calculate date range
        end_date = datetime.utcnow()
        start_date = end_date - timedelta(days=days)

        # Use existing list_platform_feedback method
        # Note: This will need tenant_id filter added to the service method
        feedback_list = review_service.list_platform_feedback(
            tenant_id=parsed_tenant_id,
            category=category,
            status=status,
            start_date=start_date,
            end_date=end_date,
            limit=limit,
            offset=offset,
        )

        return feedback_list

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid parameter: {str(e)}",
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch platform feedback: {str(e)}",
        )
