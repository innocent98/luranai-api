"""Agent app: own ratings — GET /agent/me/ratings (spec §4.3.5, N4-D5).

Own ratings only, by construction (filter is the caller's id). No customer identity
(no user_identifier, visitor name or session id). Not gated by the analytics plan
capability: an agent's own feedback is not a plan analytics feature.
"""

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.api.deps import get_current_agent
from app.db.models.review import SessionRating
from app.db.session import get_db
from app.schemas.rating import MyRatingItem, MyRatingsResponse
from app.schemas.user import UserResponse
from app.services.rating_read_service import agent_rating_stats, ticket_refs
from app.utils.datetime import ensure_utc_aware, utcnow_aware

router = APIRouter()

RECENT_LIMIT = 20
DEFAULT_WINDOW = timedelta(days=30)


def _window(start: datetime | None, end: datetime | None) -> tuple[datetime, datetime]:
    end_at = ensure_utc_aware(end) if end else utcnow_aware()
    start_at = ensure_utc_aware(start) if start else end_at - DEFAULT_WINDOW
    return start_at, end_at


@router.get("/me/ratings", response_model=MyRatingsResponse)
def get_my_ratings(
    start_date: datetime | None = Query(None, description="Window start (default end - 30 d)"),
    end_date: datetime | None = Query(None, description="Window end (default now)"),
    db: Session = Depends(get_db),
    current_agent: UserResponse = Depends(get_current_agent),
):
    start, end = _window(start_date, end_date)
    if end < start:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="End date must be after start date",
        )
    stats = agent_rating_stats(db, current_agent.tenant_id, current_agent.id, start, end)
    rows = (
        db.execute(
            select(SessionRating)
            .where(
                SessionRating.tenant_id == current_agent.tenant_id,
                SessionRating.rated_agent_id == current_agent.id,
                SessionRating.created_at >= start,
                SessionRating.created_at < end,
            )
            .order_by(desc(SessionRating.created_at))
            .limit(RECENT_LIMIT)
        )
        .scalars()
        .all()
    )
    refs = ticket_refs(db, (r.rating_request_id for r in rows), current_agent.tenant_id)
    return MyRatingsResponse(
        period_start=start,
        period_end=end,
        count=stats.count,
        average_score=stats.average,
        csat_percent=stats.csat_percent,
        recent=[
            MyRatingItem(
                id=r.id,
                score=r.score,
                comment=r.comment,
                created_at=r.created_at,
                ticket_number=refs.get(r.rating_request_id, (None, None))[1],
                source=r.source,
            )
            for r in rows
        ],
    )
