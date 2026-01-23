"""Review and rating endpoints."""

from datetime import datetime
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.orm import Session
from loguru import logger

from app.api.deps import get_current_user, get_current_tenant_admin, get_db
from app.services.review_service import ReviewService
from app.schemas.review import (
    SessionRatingCreate,
    SessionRatingResponse,
    SessionRatingListResponse,
    PlatformFeedbackCreate,
    PlatformFeedbackResponse,
    PlatformFeedbackListResponse,
)
from app.schemas.user import UserResponse
from app.db.models.review import FeedbackCategory

router = APIRouter()


# ===== Session Rating Endpoints =====


@router.post(
    "/session",
    response_model=SessionRatingResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Submit session rating",
    description="Public endpoint for customers to rate their chat experience. No authentication required.",
)
async def create_session_rating(
    rating_data: SessionRatingCreate,
    db: Session = Depends(get_db),
):
    """
    Submit a rating for a chat session.

    This is a public endpoint that allows customers to rate their experience
    after completing a chat session. The user_identifier must match the session
    to prevent unauthorized ratings.

    **Validation:**
    - Session must exist
    - User identifier must match the session
    - One rating per session (duplicate ratings return 409)
    - Score must be 1-5

    **Returns:**
    - 201: Rating created successfully
    - 400: Invalid data (session not found, user mismatch)
    - 409: Rating already exists for this session
    """
    try:
        review_service = ReviewService(db)
        rating = review_service.create_session_rating(rating_data)

        logger.info(
            f"Session rating created: session_id={rating_data.session_id}, "
            f"score={rating_data.score}"
        )

        return rating

    except ValueError as e:
        error_msg = str(e)
        if "already exists" in error_msg.lower():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=error_msg,
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error_msg,
        )
    except Exception as e:
        logger.error(f"Error creating session rating: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create session rating",
        )


@router.get(
    "/session",
    response_model=SessionRatingListResponse,
    summary="List session ratings",
    description="View customer ratings for chat sessions (tenant-scoped, requires authentication).",
)
async def list_session_ratings(
    session_id: Optional[UUID] = Query(None, description="Filter by session ID"),
    min_score: Optional[int] = Query(None, ge=1, le=5, description="Minimum rating score"),
    max_score: Optional[int] = Query(None, ge=1, le=5, description="Maximum rating score"),
    start_date: Optional[datetime] = Query(None, description="Filter from date (ISO 8601)"),
    end_date: Optional[datetime] = Query(None, description="Filter to date (ISO 8601)"),
    limit: int = Query(50, ge=1, le=100, description="Max results per page"),
    offset: int = Query(0, ge=0, description="Results offset for pagination"),
    current_user: UserResponse = Depends(get_current_tenant_admin),
    db: Session = Depends(get_db),
):
    """
    List session ratings for the authenticated tenant.

    Returns paginated list of customer ratings with filters and average score.

    **Query Parameters:**
    - session_id: Filter by specific session
    - min_score/max_score: Filter by rating range
    - start_date/end_date: Filter by date range
    - limit/offset: Pagination controls

    **Returns:**
    - List of ratings
    - Total count
    - Average score across filtered results
    - Pagination metadata
    """
    try:
        review_service = ReviewService(db)
        ratings = review_service.list_session_ratings(
            tenant_id=current_user.tenant_id,
            session_id=session_id,
            min_score=min_score,
            max_score=max_score,
            start_date=start_date,
            end_date=end_date,
            limit=limit,
            offset=offset,
        )

        return ratings

    except Exception as e:
        logger.error(f"Error listing session ratings: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve session ratings",
        )


# ===== Platform Feedback Endpoints =====


@router.post(
    "/platform",
    response_model=PlatformFeedbackResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Submit platform feedback",
    description="Submit feedback about Luran platform (requires authentication).",
)
async def create_platform_feedback(
    feedback_data: PlatformFeedbackCreate,
    current_user: UserResponse = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Submit feedback about the Luran platform.

    Allows authenticated users to provide feedback, report bugs, request features,
    or rate their satisfaction with the platform.

    **Feedback Categories:**
    - feature_request: Request new features
    - bug_report: Report bugs or issues
    - performance: Performance-related feedback
    - platform_satisfaction: Rate overall platform satisfaction (requires satisfaction_score)
    - documentation: Documentation feedback
    - pricing: Pricing-related feedback
    - general: General feedback

    **Returns:**
    - 201: Feedback created successfully
    - 400: Invalid data
    - 401: Not authenticated
    """
    try:
        review_service = ReviewService(db)
        feedback = review_service.create_platform_feedback(
            tenant_id=current_user.tenant_id,
            feedback_data=feedback_data,
            user_id=current_user.id,
        )

        logger.info(
            f"Platform feedback created: tenant_id={current_user.tenant_id}, "
            f"category={feedback_data.category}, user_id={current_user.id}"
        )

        return feedback

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
    except Exception as e:
        logger.error(f"Error creating platform feedback: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create platform feedback",
        )


@router.get(
    "/platform",
    response_model=PlatformFeedbackListResponse,
    summary="List platform feedback",
    description="View platform feedback (tenant sees own feedback, requires authentication).",
)
async def list_platform_feedback(
    category: Optional[FeedbackCategory] = Query(None, description="Filter by category"),
    status_filter: Optional[str] = Query(
        None,
        alias="status",
        pattern="^(open|acknowledged|in_progress|resolved|closed)$",
        description="Filter by status",
    ),
    start_date: Optional[datetime] = Query(None, description="Filter from date (ISO 8601)"),
    end_date: Optional[datetime] = Query(None, description="Filter to date (ISO 8601)"),
    limit: int = Query(50, ge=1, le=100, description="Max results per page"),
    offset: int = Query(0, ge=0, description="Results offset for pagination"),
    current_user: UserResponse = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    List platform feedback for the authenticated tenant.

    Returns paginated list of feedback submitted by the tenant.

    **Query Parameters:**
    - category: Filter by feedback category
    - status: Filter by feedback status
    - start_date/end_date: Filter by date range
    - limit/offset: Pagination controls

    **Returns:**
    - List of feedback items
    - Total count
    - Pagination metadata
    """
    try:
        review_service = ReviewService(db)
        feedback = review_service.list_platform_feedback(
            tenant_id=current_user.tenant_id,
            category=category,
            status=status_filter,
            start_date=start_date,
            end_date=end_date,
            limit=limit,
            offset=offset,
        )

        return feedback

    except Exception as e:
        logger.error(f"Error listing platform feedback: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve platform feedback",
        )
