"""Review and rating endpoints."""

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from loguru import logger
from sqlalchemy.orm import Session

from app.api.deps import (
    AuthContext,
    get_current_platform_admin,
    get_current_user,
    get_current_user_or_platform_admin,
    get_db,
)
from app.db.models.review import FeedbackCategory
from app.schemas.platform_admin import PlatformAdminResponse
from app.schemas.review import (
    PlatformFeedbackCreate,
    PlatformFeedbackListResponse,
    PlatformFeedbackResponse,
    PublicFeedbackCreate,
    PublicFeedbackListResponse,
    PublicFeedbackResponse,
    SessionRatingCreate,
    SessionRatingListResponse,
    SessionRatingSubmitResponse,
)
from app.schemas.user import UserResponse
from app.services.review_service import RatingSubmitError, ReviewService
from app.utils.http_rate_limiter import rating_rate_limit

router = APIRouter()


# ===== Session Rating Endpoints =====


@router.post(
    "/session",
    response_model=SessionRatingSubmitResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(rating_rate_limit)],
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
    - 400: Invalid data (session not found, user mismatch) or `rating_request_invalid`
    - 409: Rating already exists for this session, or `already_rated` (token path)
    - 410: `rating_request_expired`
    - 429: `rating_rate_limited`
    """
    try:
        review_service = ReviewService(db)
        rating = review_service.create_session_rating(rating_data)

        logger.info(
            f"Session rating created: session_id={rating_data.session_id}, "
            f"score={rating_data.score}, tokened={bool(rating_data.rating_request_token)}"
        )

        return rating

    except RatingSubmitError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail()) from e
    except ValueError as e:
        error_msg = str(e)
        if "already exists" in error_msg.lower():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=error_msg,
            ) from e
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error_msg,
        ) from e
    except Exception as e:
        logger.error(f"Error creating session rating: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create session rating",
        ) from e


@router.get(
    "/session",
    response_model=SessionRatingListResponse,
    summary="List session ratings",
    description="View customer ratings (tenant users see own ratings, platform admins see all)",
)
async def list_session_ratings(
    session_id: UUID | None = Query(None, description="Filter by session ID"),
    min_score: int | None = Query(None, ge=1, le=5, description="Minimum rating score"),
    max_score: int | None = Query(None, ge=1, le=5, description="Maximum rating score"),
    start_date: datetime | None = Query(None, description="Filter from date (ISO 8601)"),
    end_date: datetime | None = Query(None, description="Filter to date (ISO 8601)"),
    limit: int = Query(50, ge=1, le=100, description="Max results per page"),
    offset: int = Query(0, ge=0, description="Results offset for pagination"),
    auth: AuthContext = Depends(get_current_user_or_platform_admin),
    db: Session = Depends(get_db),
):
    """
    List session ratings with dual authentication.

    **Access Control:**
    - Tenant users (any role): View ratings for their own tenant only
    - Platform admins: View ratings across ALL tenants

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

        # Determine tenant scope based on auth type
        if auth.auth_type == "tenant_user":
            # Tenant user: filter by their tenant
            tenant_id = auth.tenant_id
            logger.info(f"Tenant user viewing ratings for tenant {tenant_id}")
        else:
            # Platform admin: no tenant filter (view all)
            tenant_id = None
            logger.info("Platform admin viewing all ratings (system-wide)")

        ratings = review_service.list_session_ratings(
            tenant_id=tenant_id,
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
        ) from e


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
        ) from e
    except Exception as e:
        logger.error(f"Error creating platform feedback: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create platform feedback",
        ) from e


@router.get(
    "/platform",
    response_model=PlatformFeedbackListResponse,
    summary="List platform feedback",
    description="View platform feedback (platform admins only, system-wide view)",
)
async def list_platform_feedback(
    category: FeedbackCategory | None = Query(None, description="Filter by category"),
    status_filter: str | None = Query(
        None,
        alias="status",
        pattern="^(open|acknowledged|in_progress|resolved|closed)$",
        description="Filter by status",
    ),
    start_date: datetime | None = Query(None, description="Filter from date (ISO 8601)"),
    end_date: datetime | None = Query(None, description="Filter to date (ISO 8601)"),
    limit: int = Query(50, ge=1, le=100, description="Max results per page"),
    offset: int = Query(0, ge=0, description="Results offset for pagination"),
    current_admin: PlatformAdminResponse = Depends(get_current_platform_admin),
    db: Session = Depends(get_db),
):
    """
    List platform feedback (platform admins only).

    **Access Control:**
    - ONLY platform admins can view feedback
    - Returns ALL feedback across ALL tenants (system-wide)

    **Note:** Tenant users can still SUBMIT feedback via POST /reviews/platform,
    but they cannot view feedback (their own or others).

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

        logger.info("Platform admin viewing all platform feedback (system-wide)")

        feedback = review_service.list_platform_feedback(
            tenant_id=None,
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
        ) from e


# ===== Public Landing Page Feedback Endpoints =====


@router.post(
    "/public-feedback",
    response_model=PublicFeedbackResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Submit public feedback from landing page",
    description="PUBLIC endpoint - No authentication required. Submit feedback about Luran platform.",
)
async def create_public_feedback(
    feedback_data: PublicFeedbackCreate,
    db: Session = Depends(get_db),
):
    """
    Submit public feedback about Luran platform (no authentication required).

    This is a PUBLIC endpoint accessible from the landing page for anyone to
    provide feedback, report issues, or request features.

    **Feedback Categories:**
    - feature_request: Request new features
    - bug_report: Report bugs or issues
    - performance: Performance-related feedback
    - platform_satisfaction: Rate overall platform satisfaction
    - documentation: Documentation feedback
    - pricing: Pricing-related feedback
    - general: General feedback

    **Optional Contact Information:**
    - name: Submitter name (for follow-up)
    - email: Contact email (for follow-up)
    - company: Company/organization name

    **Returns:**
    - 201: Feedback created successfully
    - 400: Invalid data
    """
    try:
        review_service = ReviewService(db)
        feedback = review_service.create_public_feedback(feedback_data)

        logger.info(
            f"Public feedback created: category={feedback_data.category}, "
            f"email={feedback_data.email or 'anonymous'}"
        )

        return feedback

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        ) from e
    except Exception as e:
        logger.error(f"Error creating public feedback: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create public feedback",
        ) from e


@router.get(
    "/public-feedback",
    response_model=PublicFeedbackListResponse,
    summary="List public feedback",
    description="View public landing page feedback (platform admins only)",
)
async def list_public_feedback(
    category: FeedbackCategory | None = Query(None, description="Filter by category"),
    status_filter: str | None = Query(
        None,
        alias="status",
        pattern="^(open|acknowledged|in_progress|resolved|closed)$",
        description="Filter by status",
    ),
    start_date: datetime | None = Query(None, description="Filter from date (ISO 8601)"),
    end_date: datetime | None = Query(None, description="Filter to date (ISO 8601)"),
    limit: int = Query(50, ge=1, le=100, description="Max results per page"),
    offset: int = Query(0, ge=0, description="Results offset for pagination"),
    current_admin: PlatformAdminResponse = Depends(get_current_platform_admin),
    db: Session = Depends(get_db),
):
    """
    List public landing page feedback (platform admins only).

    **Access Control:**
    - ONLY platform admins can view public feedback
    - Returns ALL public feedback from landing page visitors

    **Query Parameters:**
    - category: Filter by feedback category
    - status: Filter by feedback status
    - start_date/end_date: Filter by date range
    - limit/offset: Pagination controls

    **Returns:**
    - List of public feedback items
    - Total count
    - Pagination metadata
    """
    try:
        review_service = ReviewService(db)

        logger.info("Platform admin viewing all public landing page feedback")

        feedback = review_service.list_public_feedback(
            category=category,
            status=status_filter,
            start_date=start_date,
            end_date=end_date,
            limit=limit,
            offset=offset,
        )

        return feedback

    except Exception as e:
        logger.error(f"Error listing public feedback: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve public feedback",
        ) from e
