"""Review service for managing session ratings and platform feedback."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import and_, desc, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models.conversation import ConversationSession
from app.db.models.review import FeedbackCategory, PlatformFeedback, PublicFeedback, SessionRating
from app.schemas.review import (
    PlatformFeedbackCreate,
    PlatformFeedbackListResponse,
    PlatformFeedbackResponse,
    PlatformFeedbackUpdate,
    PublicFeedbackCreate,
    PublicFeedbackListResponse,
    PublicFeedbackResponse,
    SessionRatingCreate,
    SessionRatingListResponse,
    SessionRatingResponse,
)


class ReviewService:
    """Service for session ratings and platform feedback management."""

    def __init__(self, db: Session):
        """Initialize review service with database session."""
        self.db = db

    # ===== Session Rating Methods =====

    def create_session_rating(self, rating_data: SessionRatingCreate) -> SessionRatingResponse:
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
        stmt = select(ConversationSession).where(ConversationSession.id == rating_data.session_id)
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
        except IntegrityError as err:
            self.db.rollback()
            raise ValueError("Rating already exists for this session") from err

        return SessionRatingResponse.model_validate(db_rating)

    def get_session_rating(self, session_id: UUID) -> SessionRatingResponse | None:
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
        tenant_id: UUID | None = None,
        session_id: UUID | None = None,
        min_score: int | None = None,
        max_score: int | None = None,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> SessionRatingListResponse:
        """
        List session ratings with optional tenant filtering.

        Args:
            tenant_id: Filter by tenant (required for tenant users, None for platform admins)
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
        filters = []

        # Only add tenant filter if tenant_id is provided
        if tenant_id is not None:
            filters.append(SessionRating.tenant_id == tenant_id)

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
        if filters:
            count_stmt = select(func.count(SessionRating.id)).where(and_(*filters))
        else:
            count_stmt = select(func.count(SessionRating.id))
        total = self.db.execute(count_stmt).scalar() or 0

        # Get average score
        if filters:
            avg_stmt = select(func.avg(SessionRating.score)).where(and_(*filters))
        else:
            avg_stmt = select(func.avg(SessionRating.score))
        average_score = self.db.execute(avg_stmt).scalar()

        # Get ratings
        if filters:
            stmt = (
                select(SessionRating)
                .where(and_(*filters))
                .order_by(desc(SessionRating.created_at))
                .limit(limit)
                .offset(offset)
            )
        else:
            stmt = (
                select(SessionRating)
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
        self, tenant_id: UUID, feedback_data: PlatformFeedbackCreate, user_id: UUID | None = None
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

    def get_platform_feedback(self, feedback_id: UUID) -> PlatformFeedbackResponse | None:
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
        tenant_id: UUID | None = None,
        category: FeedbackCategory | None = None,
        status: str | None = None,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
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

    # ===== Public Landing Page Feedback Methods =====

    def create_public_feedback(self, feedback_data: PublicFeedbackCreate) -> PublicFeedbackResponse:
        """
        Create public feedback from landing page (no authentication required).

        Args:
            feedback_data: Public feedback creation data

        Returns:
            Created feedback response
        """
        db_feedback = PublicFeedback(
            category=feedback_data.category,
            satisfaction_score=feedback_data.satisfaction_score,
            title=feedback_data.title,
            description=feedback_data.description,
            name=feedback_data.name,
            email=feedback_data.email,
            company=feedback_data.company,
            status="open",
            meta=feedback_data.meta or {},
        )

        self.db.add(db_feedback)
        self.db.commit()
        self.db.refresh(db_feedback)

        return PublicFeedbackResponse.model_validate(db_feedback)

    def list_public_feedback(
        self,
        category: FeedbackCategory | None = None,
        status: str | None = None,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> PublicFeedbackListResponse:
        """
        List public feedback (platform admins only).

        Args:
            category: Filter by feedback category
            status: Filter by status
            start_date: Filter from date
            end_date: Filter to date
            limit: Max results (default 50)
            offset: Results offset (default 0)

        Returns:
            Paginated list of public feedback
        """
        # Build filters
        filters = []

        if category:
            filters.append(PublicFeedback.category == category)
        if status:
            filters.append(PublicFeedback.status == status)
        if start_date:
            filters.append(PublicFeedback.created_at >= start_date)
        if end_date:
            filters.append(PublicFeedback.created_at <= end_date)

        # Get total count
        if filters:
            count_stmt = select(func.count(PublicFeedback.id)).where(and_(*filters))
        else:
            count_stmt = select(func.count(PublicFeedback.id))
        total = self.db.execute(count_stmt).scalar() or 0

        # Get feedback
        if filters:
            stmt = (
                select(PublicFeedback)
                .where(and_(*filters))
                .order_by(desc(PublicFeedback.created_at))
                .limit(limit)
                .offset(offset)
            )
        else:
            stmt = (
                select(PublicFeedback)
                .order_by(desc(PublicFeedback.created_at))
                .limit(limit)
                .offset(offset)
            )

        feedback_list = self.db.execute(stmt).scalars().all()

        return PublicFeedbackListResponse(
            feedback=[PublicFeedbackResponse.model_validate(f) for f in feedback_list],
            total=total,
            page=(offset // limit) + 1 if limit > 0 else 1,
            page_size=limit,
        )

    # ===== Platform-Wide Analytics Methods =====

    def get_all_session_ratings(
        self,
        tenant_id: UUID | None = None,
        min_score: int | None = None,
        max_score: int | None = None,
        days: int = 30,
        limit: int = 100,
        offset: int = 0,
    ) -> dict:
        """
        Get session ratings across all tenants (platform admin only).

        Args:
            tenant_id: Optional tenant filter
            min_score: Minimum score filter
            max_score: Maximum score filter
            days: Number of days to look back
            limit: Max results
            offset: Results offset

        Returns:
            Dict with ratings and statistics
        """
        from datetime import datetime, timedelta

        end_date = datetime.utcnow()
        start_date = end_date - timedelta(days=days)

        # Build filters
        filters = [SessionRating.created_at >= start_date]

        if tenant_id:
            filters.append(SessionRating.tenant_id == tenant_id)
        if min_score is not None:
            filters.append(SessionRating.score >= min_score)
        if max_score is not None:
            filters.append(SessionRating.score <= max_score)

        # Get total count
        count_stmt = select(func.count(SessionRating.id)).where(and_(*filters))
        total = self.db.execute(count_stmt).scalar() or 0

        # Get ratings
        stmt = (
            select(SessionRating)
            .where(and_(*filters))
            .order_by(desc(SessionRating.created_at))
            .limit(limit)
            .offset(offset)
        )

        ratings = self.db.execute(stmt).scalars().all()

        # Calculate statistics
        avg_stmt = select(func.avg(SessionRating.score)).where(and_(*filters))
        avg_score = self.db.execute(avg_stmt).scalar() or 0

        # Score distribution
        score_dist_stmt = (
            select(SessionRating.score, func.count(SessionRating.id).label("count"))
            .where(and_(*filters))
            .group_by(SessionRating.score)
        )
        score_dist = self.db.execute(score_dist_stmt).all()

        return {
            "ratings": [SessionRatingResponse.model_validate(r) for r in ratings],
            "statistics": {
                "total_ratings": total,
                "average_score": float(avg_score),
                "score_distribution": {int(score): int(count) for score, count in score_dist},
            },
            "pagination": {
                "limit": limit,
                "offset": offset,
                "total": total,
            },
        }

    def analyze_sentiment(
        self,
        tenant_id: UUID | None = None,
        days: int = 30,
    ) -> dict:
        """
        Analyze sentiment from ratings and feedback.

        Args:
            tenant_id: Optional tenant filter
            days: Number of days to analyze

        Returns:
            Dict with sentiment analysis
        """
        from datetime import datetime, timedelta

        end_date = datetime.utcnow()
        start_date = end_date - timedelta(days=days)

        # Session ratings sentiment
        rating_filters = [SessionRating.created_at >= start_date]
        if tenant_id:
            rating_filters.append(SessionRating.tenant_id == tenant_id)

        # Count by score ranges (positive, neutral, negative)
        positive_stmt = select(func.count(SessionRating.id)).where(
            and_(SessionRating.score >= 4, *rating_filters)
        )
        neutral_stmt = select(func.count(SessionRating.id)).where(
            and_(SessionRating.score == 3, *rating_filters)
        )
        negative_stmt = select(func.count(SessionRating.id)).where(
            and_(SessionRating.score <= 2, *rating_filters)
        )

        positive_count = self.db.execute(positive_stmt).scalar() or 0
        neutral_count = self.db.execute(neutral_stmt).scalar() or 0
        negative_count = self.db.execute(negative_stmt).scalar() or 0

        total_ratings = positive_count + neutral_count + negative_count

        # Platform feedback sentiment
        feedback_filters = [PlatformFeedback.created_at >= start_date]
        if tenant_id:
            feedback_filters.append(PlatformFeedback.tenant_id == tenant_id)

        # Count by satisfaction score
        satisfied_stmt = select(func.count(PlatformFeedback.id)).where(
            and_(PlatformFeedback.satisfaction_score >= 4, *feedback_filters)
        )
        unsatisfied_stmt = select(func.count(PlatformFeedback.id)).where(
            and_(PlatformFeedback.satisfaction_score <= 2, *feedback_filters)
        )

        satisfied_count = self.db.execute(satisfied_stmt).scalar() or 0
        unsatisfied_count = self.db.execute(unsatisfied_stmt).scalar() or 0

        # Calculate overall sentiment score (0-100)
        if total_ratings > 0:
            sentiment_score = (
                (positive_count * 1.0 + neutral_count * 0.5 + negative_count * 0.0)
                / total_ratings
                * 100
            )
        else:
            sentiment_score = 50  # Neutral default

        return {
            "period_days": days,
            "session_ratings": {
                "total": total_ratings,
                "positive": positive_count,
                "neutral": neutral_count,
                "negative": negative_count,
                "positive_percentage": (
                    (positive_count / total_ratings * 100) if total_ratings > 0 else 0
                ),
            },
            "platform_feedback": {
                "satisfied": satisfied_count,
                "unsatisfied": unsatisfied_count,
            },
            "overall_sentiment_score": float(sentiment_score),
            "sentiment_category": (
                "positive"
                if sentiment_score >= 70
                else "neutral" if sentiment_score >= 40 else "negative"
            ),
        }

    def calculate_tenant_health_scores(
        self,
        days: int = 30,
        limit: int = 50,
    ) -> dict:
        """
        Calculate health scores for tenants based on feedback.

        Args:
            days: Number of days to analyze
            limit: Max tenants to return

        Returns:
            Dict with tenant health scores
        """
        from datetime import datetime, timedelta

        from app.db.models.tenant import Tenant

        end_date = datetime.utcnow()
        start_date = end_date - timedelta(days=days)

        # Get all active tenants
        tenant_stmt = select(Tenant).where(Tenant.subscription_status.in_(["active", "trialing"]))
        tenants = self.db.execute(tenant_stmt).scalars().all()

        tenant_scores: list[dict[str, Any]] = []

        for tenant in tenants:
            # Get average session rating
            avg_rating_stmt = select(func.avg(SessionRating.score)).where(
                and_(
                    SessionRating.tenant_id == tenant.id,
                    SessionRating.created_at >= start_date,
                )
            )
            avg_rating = self.db.execute(avg_rating_stmt).scalar() or 0

            # Get rating count
            rating_count_stmt = select(func.count(SessionRating.id)).where(
                and_(
                    SessionRating.tenant_id == tenant.id,
                    SessionRating.created_at >= start_date,
                )
            )
            rating_count = self.db.execute(rating_count_stmt).scalar() or 0

            # Get platform feedback satisfaction
            avg_satisfaction_stmt = select(func.avg(PlatformFeedback.satisfaction_score)).where(
                and_(
                    PlatformFeedback.tenant_id == tenant.id,
                    PlatformFeedback.created_at >= start_date,
                )
            )
            avg_satisfaction = self.db.execute(avg_satisfaction_stmt).scalar() or 0

            # Calculate health score (0-100)
            # Weight: 60% session ratings + 40% satisfaction
            if rating_count > 0 or avg_satisfaction > 0:
                health_score = (avg_rating / 5 * 60) + (avg_satisfaction / 5 * 40)
            else:
                health_score = 0  # No data

            tenant_scores.append(
                {
                    "tenant_id": str(tenant.id),
                    "tenant_name": tenant.name,
                    "display_name": tenant.display_name,
                    "health_score": float(health_score),
                    "health_category": (
                        "excellent"
                        if health_score >= 80
                        else (
                            "good"
                            if health_score >= 60
                            else (
                                "fair"
                                if health_score >= 40
                                else "poor" if health_score > 0 else "no_data"
                            )
                        )
                    ),
                    "metrics": {
                        "average_rating": float(avg_rating),
                        "rating_count": rating_count,
                        "average_satisfaction": float(avg_satisfaction),
                    },
                }
            )

        # Sort by health score (descending)
        tenant_scores.sort(key=lambda x: float(x.get("health_score", 0)), reverse=True)

        return {
            "period_days": days,
            "total_tenants": len(tenant_scores),
            "tenants": tenant_scores[:limit],
        }
