"""Read-side helpers for N4 ratings: names, ticket refs, briefs (stats added in Task 8)."""

from collections.abc import Iterable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.conversation import Ticket
from app.db.models.review import RatingRequest, SessionRating
from app.db.models.user import User
from app.schemas.rating import SessionRatingBrief


def rated_party(rated_agent_id: UUID | None) -> str:
    return "human" if rated_agent_id else "ai"


def agent_names(db: Session, ids: Iterable[UUID | None]) -> dict[UUID, str]:
    wanted = {i for i in ids if i}
    if not wanted:
        return {}
    users = db.execute(select(User).where(User.id.in_(wanted))).scalars()
    return {u.id: u.full_name for u in users}


def ticket_refs(db: Session, request_ids: Iterable[UUID | None]) -> dict[UUID, tuple[UUID, str]]:
    wanted = {i for i in request_ids if i}
    if not wanted:
        return {}
    rows = db.execute(
        select(RatingRequest.id, Ticket.id, Ticket.ticket_number)
        .join(Ticket, Ticket.id == RatingRequest.ticket_id)
        .where(RatingRequest.id.in_(wanted))
    ).all()
    return {rid: (tid, number) for rid, tid, number in rows}


def to_brief(rating: SessionRating, names: dict[UUID, str]) -> SessionRatingBrief:
    return SessionRatingBrief(
        id=rating.id,
        score=rating.score,
        comment=rating.comment,
        created_at=rating.created_at,
        rated_party=rated_party(rating.rated_agent_id),
        rated_agent_id=rating.rated_agent_id,
        rated_agent_name=names.get(rating.rated_agent_id) if rating.rated_agent_id else None,
        source=rating.source,
    )


def session_rating_briefs(db: Session, session_id: UUID) -> list[SessionRatingBrief]:
    rows = (
        db.execute(
            select(SessionRating)
            .where(SessionRating.session_id == session_id)
            .order_by(SessionRating.created_at)
        )
        .scalars()
        .all()
    )
    names = agent_names(db, (r.rated_agent_id for r in rows))
    return [to_brief(r, names) for r in rows]
