"""Review service for managing session ratings and platform feedback."""

from datetime import datetime
from typing import Optional
from uuid import UUID

from sqlalchemy.orm import Session
from sqlalchemy import select, desc, func, and_
from sqlalchemy.exc import IntegrityError

from app.db.models.review import (
    SessionRating,
    PlatformFeedback,
    FeedbackCategory,
)
from app.db.models.conversation import ConversationSession
from app.schemas.review import (
    SessionRatingCreate,
    SessionRatingResponse,
    SessionRatingListResponse,
    PlatformFeedbackCreate,
    PlatformFeedbackResponse,
    PlatformFeedbackListResponse,
    PlatformFeedbackUpdate,
)


class ReviewService:
    """Service for session ratings and platform feedback management."""

    def __init__(self, db: Session):
        """Initialize review service with database session."""
        self.db = db

    # ===== Session Rating Methods =====

    def create_session_rating(
        self, rating_data: SessionRatingCreate
    ) -> SessionRatingResponse:
        """
        Create a new session rating.

        Args:
            rating_data: Rating creation data

        Returns:
            Created rating response

        Raises:
            ValueError: If session not found or user_identifier doesn't match
            IntegrityError: If rating already exists for this session
        """
        # Verify session exists and get tenant_id
        stmt = select(ConversationSession).where(
            ConversationSession.id == rating_data.session_id
        )
        session = self.db.execute(stmt).scalar_one_or_none()

        if not session:
            raise ValueError("Session not found")

        # Verify user_identifier matches session
        if session.user_identifier != rating_data.user_identifier:
            raise ValueError("User identifier does not match session")

        # Create rating
        db_rating = SessionRating(
            tenant_id=session.tenant_id,
            session_id=rating_data.session_id,
            score=rating_data.score,
            comment=rating_data.comment,
            user_identifier=rating_data.user_identifier,
            meta=rating_data.meta or {},
        )

        try:
            self.db.add(db_rating)
            self.db.commit()
            self.db.refresh(db_rating)
        except IntegrityError:
            self.db.rollback()
            raise ValueError("Rating already exists for this session")

        return SessionRatingResponse.model_validate(db_rating)

    def get_session_rating(
        self, session_id: UUID
    ) -> Optional[SessionRatingResponse]:
        """
        Get rating for a specific session.

        Args:
            session_id: Session UUID

        Returns:
            Rating response if found, None otherwise
        """
        stmt = select(SessionRating).where(SessionRating.session_id == session_id)
        rating = self.db.execute(stmt).scalar_one_or_none()

        if not rating:
            return None

        return SessionRatingResponse.model_validate(rating)

    def list_session_ratings(
        self,
        tenant_id: UUID,
        session_id: Optional[UUID] = None,
        min_score: Optional[int] = None,
        max_score: Optional[int] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> SessionRatingListResponse:
        """
        List session ratings for a tenant with filters.

        Args:
            tenant_id: Tenant UUID
            session_id: Filter by session ID
            min_score: Minimum rating score
            max_score: Maximum rating score
            start_date: Filter from date
            end_date: Filter to date
            limit: Max results (default 50)
            offset: Results offset (default 0)

        Returns:
            Paginated list of ratings with average score
        """
        # Build filters
        filters = [SessionRating.tenant_id == tenant_id]

        if session_id:
            filters.append(SessionRating.session_id == session_id)
        if min_score is not None:
            filters.append(SessionRating.score >= min_score)
        if max_score is not None:
            filters.append(SessionRating.score <= max_score)
        if start_date:
            filters.append(SessionRating.created_at >= start_date)
        if end_date:
            filters.append(SessionRating.created_at <= end_date)

        # Get total count
        count_stmt = select(func.count(SessionRating.id)).where(and_(*filters))
        total = self.db.execute(count_stmt).scalar() or 0

        # Get average score
        avg_stmt = select(func.avg(SessionRating.score)).where(and_(*filters))
        average_score = self.db.execute(avg_stmt).scalar()

        # Get ratings
        stmt = (
            select(SessionRating)
            .where(and_(*filters))
            .order_by(desc(SessionRating.created_at))
            .limit(limit)
            .offset(offset)
        )
        ratings = self.db.execute(stmt).scalars().all()

        return SessionRatingListResponse(
            ratings=[SessionRatingResponse.model_validate(r) for r in ratings],
            total=total,
            average_score=float(average_score) if average_score else None,
            page=(offset // limit) + 1 if limit > 0 else 1,
            page_size=limit,
        )

    # ===== Platform Feedback Methods =====

    def create_platform_feedback(
        self, tenant_id: UUID, feedback_data: PlatformFeedbackCreate, user_id: Optional[UUID] = None
    ) -> PlatformFeedbackResponse:
        """
        Create platform feedback.

        Args:
            tenant_id: Tenant UUID
            feedback_data: Feedback creation data
            user_id: Optional user ID who submitted

        Returns:
            Created feedback response
        """
        db_feedback = PlatformFeedback(
            tenant_id=tenant_id,
            category=feedback_data.category,
            satisfaction_score=feedback_data.satisfaction_score,
            title=feedback_data.title,
            description=feedback_data.description,
            status="open",
            submitted_by=user_id,
            meta=feedback_data.meta or {},
        )

        self.db.add(db_feedback)
        self.db.commit()
        self.db.refresh(db_feedback)

        return PlatformFeedbackResponse.model_validate(db_feedback)

    def get_platform_feedback(
        self, feedback_id: UUID
    ) -> Optional[PlatformFeedbackResponse]:
        """
        Get platform feedback by ID.

        Args:
            feedback_id: Feedback UUID

        Returns:
            Feedback response if found, None otherwise
        """
        stmt = select(PlatformFeedback).where(PlatformFeedback.id == feedback_id)
        feedback = self.db.execute(stmt).scalar_one_or_none()

        if not feedback:
            return None

        return PlatformFeedbackResponse.model_validate(feedback)

    def list_platform_feedback(
        self,
        tenant_id: Optional[UUID] = None,
        category: Optional[FeedbackCategory] = None,
        status: Optional[str] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> PlatformFeedbackListResponse:
        """
        List platform feedback with filters.

        Args:
            tenant_id: Filter by tenant (required unless admin)
            category: Filter by feedback category
            status: Filter by status
            start_date: Filter from date
            end_date: Filter to date
            limit: Max results (default 50)
            offset: Results offset (default 0)

        Returns:
            Paginated list of feedback
        """
        # Build filters
        filters = []

        if tenant_id:
            filters.append(PlatformFeedback.tenant_id == tenant_id)
        if category:
            filters.append(PlatformFeedback.category == category)
        if status:
            filters.append(PlatformFeedback.status == status)
        if start_date:
            filters.append(PlatformFeedback.created_at >= start_date)
        if end_date:
            filters.append(PlatformFeedback.created_at <= end_date)

        # Get total count
        if filters:
            count_stmt = select(func.count(PlatformFeedback.id)).where(and_(*filters))
        else:
            count_stmt = select(func.count(PlatformFeedback.id))
        total = self.db.execute(count_stmt).scalar() or 0

        # Get feedback
        if filters:
            stmt = (
                select(PlatformFeedback)
                .where(and_(*filters))
                .order_by(desc(PlatformFeedback.created_at))
                .limit(limit)
                .offset(offset)
            )
        else:
            stmt = (
                select(PlatformFeedback)
                .order_by(desc(PlatformFeedback.created_at))
                .limit(limit)
                .offset(offset)
            )

        feedback_list = self.db.execute(stmt).scalars().all()

        return PlatformFeedbackListResponse(
            feedback=[PlatformFeedbackResponse.model_validate(f) for f in feedback_list],
            total=total,
            page=(offset // limit) + 1 if limit > 0 else 1,
            page_size=limit,
        )

    def update_platform_feedback(
        self, feedback_id: UUID, feedback_data: PlatformFeedbackUpdate
    ) -> PlatformFeedbackResponse:
        """
        Update platform feedback (admin only).

        Args:
            feedback_id: Feedback UUID
            feedback_data: Updated feedback data

        Returns:
            Updated feedback response

        Raises:
            ValueError: If feedback not found
        """
        stmt = select(PlatformFeedback).where(PlatformFeedback.id == feedback_id)
        feedback = self.db.execute(stmt).scalar_one_or_none()

        if not feedback:
            raise ValueError("Feedback not found")

        # Update fields
        update_data = feedback_data.model_dump(exclude_unset=True)
        for field, value in update_data.items():
            setattr(feedback, field, value)

        self.db.commit()
        self.db.refresh(feedback)

        return PlatformFeedbackResponse.model_validate(feedback)
