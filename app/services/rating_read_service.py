"""Read-side helpers for N4 ratings: names, ticket refs, briefs (stats added in Task 8)."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.db.models.conversation import ConversationSession, Ticket
from app.db.models.review import RatingRequest, SessionRating
from app.db.models.user import User
from app.db.models.visitor import Visitor
from app.schemas.rating import SessionRatingBrief
from app.schemas.review import SessionRatingResponse
from app.utils.datetime import ensure_utc_aware
from app.utils.staff_redaction import customer_label


def rated_party(rated_agent_id: UUID | None) -> str:
    return "human" if rated_agent_id else "ai"


def agent_names(db: Session, ids: Iterable[UUID | None]) -> dict[UUID, str]:
    wanted = {i for i in ids if i}
    if not wanted:
        return {}
    users = db.execute(select(User).where(User.id.in_(wanted))).scalars()
    return {u.id: u.full_name for u in users}


def ticket_refs(
    db: Session, request_ids: Iterable[UUID | None], tenant_id: UUID | None = None
) -> dict[UUID, tuple[UUID, str]]:
    wanted = {i for i in request_ids if i}
    if not wanted:
        return {}
    stmt = (
        select(RatingRequest.id, Ticket.id, Ticket.ticket_number)
        .join(Ticket, Ticket.id == RatingRequest.ticket_id)
        .where(RatingRequest.id.in_(wanted))
    )
    if tenant_id is not None:
        stmt = stmt.where(RatingRequest.tenant_id == tenant_id, Ticket.tenant_id == tenant_id)
    rows = db.execute(stmt).all()
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


def csat_percent_expr():
    """AVG of 100/0 per rating = % of ratings scoring >= 4 (N4-D6)."""
    return func.avg(case((SessionRating.score >= 4, 100.0), else_=0.0))


def customer_labels(db: Session, session_ids: Iterable[UUID]) -> dict[UUID, str]:
    """Non-secret customer labels per session (two batched queries, no N+1)."""
    wanted = set(session_ids)
    if not wanted:
        return {}
    visitor_of = dict(
        db.execute(
            select(ConversationSession.id, ConversationSession.visitor_id).where(
                ConversationSession.id.in_(wanted)
            )
        ).all()
    )
    visitors = {
        v.id: v
        for v in db.execute(
            select(Visitor).where(Visitor.id.in_({vid for vid in visitor_of.values() if vid}))
        ).scalars()
    }
    labels = {}
    for sid in wanted:
        v = visitors.get(visitor_of.get(sid))
        labels[sid] = customer_label(
            sid,
            name=v.name if v else None,
            email=v.email if v else None,
            phone=v.phone if v else None,
        )
    return labels


def enrich_ratings(db: Session, ratings: list[SessionRating]) -> list[SessionRatingResponse]:
    names = agent_names(db, (r.rated_agent_id for r in ratings))
    refs = ticket_refs(db, (r.rating_request_id for r in ratings))
    labels = customer_labels(db, (r.session_id for r in ratings))
    items = []
    for rating in ratings:
        item = SessionRatingResponse.model_validate(rating)
        item.customer_label = labels[rating.session_id]
        item.rated_agent_name = names.get(rating.rated_agent_id) if rating.rated_agent_id else None
        item.ticket_id, item.ticket_number = refs.get(rating.rating_request_id, (None, None))
        items.append(item)
    return items


@dataclass(frozen=True)
class RatingStats:
    count: int = 0
    average: float | None = None
    csat_percent: float | None = None


NO_RATINGS = RatingStats()


def _to_stats(count, average, csat) -> RatingStats:
    if not count:
        return NO_RATINGS
    return RatingStats(int(count), round(float(average), 2), round(float(csat), 1))


def _in_window(stmt, start: datetime, end: datetime):
    """Window on the rating's own created_at (N4-D6): [start, end)."""
    return stmt.where(
        SessionRating.created_at >= ensure_utc_aware(start),
        SessionRating.created_at < ensure_utc_aware(end),
    )


def per_agent_rating_stats(
    db: Session, tenant_id: UUID, start: datetime, end: datetime
) -> dict[UUID, RatingStats]:
    """One grouped query for every rated agent in the tenant (no N+1)."""
    stmt = (
        select(
            SessionRating.rated_agent_id,
            func.count(SessionRating.id),
            func.avg(SessionRating.score),
            csat_percent_expr(),
        )
        .where(SessionRating.tenant_id == tenant_id, SessionRating.rated_agent_id.isnot(None))
        .group_by(SessionRating.rated_agent_id)
    )
    rows = db.execute(_in_window(stmt, start, end)).all()
    return {agent_id: _to_stats(c, a, s) for agent_id, c, a, s in rows}


def agent_rating_stats(
    db: Session, tenant_id: UUID, agent_id: UUID, start: datetime, end: datetime
) -> RatingStats:
    stmt = select(
        func.count(SessionRating.id), func.avg(SessionRating.score), csat_percent_expr()
    ).where(SessionRating.tenant_id == tenant_id, SessionRating.rated_agent_id == agent_id)
    count, average, csat = db.execute(_in_window(stmt, start, end)).one()
    return _to_stats(count, average, csat)
