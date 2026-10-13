"""CSAT math (N4-D6) and analytics per-agent fields (§4.3.6)."""

from datetime import timedelta

from app.db.models.review import RatingSource
from app.services.rating_read_service import (
    NO_RATINGS,
    agent_rating_stats,
    per_agent_rating_stats,
)
from app.utils.datetime import utcnow_aware, utcnow_naive
from tests.rating.conftest import headers, make_rating, make_ticket


def _rate(db, tenant, agent_id, score, *, days_ago=0):
    t = make_ticket(db, tenant, assigned_to=agent_id)
    return make_rating(
        db,
        session_id=t.session_id,
        tenant_id=tenant.id,
        score=score,
        rated_agent_id=agent_id,
        source=RatingSource.AUTO_RESOLVE,
        created_at=utcnow_naive() - timedelta(days=days_ago, minutes=1),
    )


def _window():
    end = utcnow_aware()
    return end - timedelta(days=30), end


def test_grouped_stats_math_window_and_ai_excluded(db, tenant, agent, other_agent):
    for score in (5, 4, 2):
        _rate(db, tenant, agent.id, score)
    _rate(db, tenant, agent.id, 1, days_ago=40)  # outside the window
    t = make_ticket(db, tenant)
    make_rating(db, session_id=t.session_id, tenant_id=tenant.id, score=1)  # AI
    stats = per_agent_rating_stats(db, tenant.id, *_window())
    assert stats[agent.id].count == 3
    assert stats[agent.id].average == 3.67
    assert stats[agent.id].csat_percent == 66.7
    assert other_agent.id not in stats
    assert agent_rating_stats(db, tenant.id, other_agent.id, *_window()) == NO_RATINGS


def test_analytics_agents_rows_carry_rating_fields(client, db, tenant, agent, other_agent, admin):
    _rate(db, tenant, agent.id, 5)
    _rate(db, tenant, agent.id, 3)
    rows = {
        r["agent_id"]: r
        for r in client.get("/api/v1/analytics/agents", headers=headers(admin)).json()
    }
    assert (rows[str(agent.id)]["ratings_count"], rows[str(agent.id)]["average_rating"]) == (2, 4.0)
    assert rows[str(agent.id)]["csat_percent"] == 50.0
    empty = rows[str(other_agent.id)]
    assert (empty["ratings_count"], empty["average_rating"], empty["csat_percent"]) == (
        0,
        None,
        None,
    )


def test_analytics_agent_detail_for_self(client, db, tenant, agent):
    _rate(db, tenant, agent.id, 4)
    body = client.get(f"/api/v1/analytics/agents/{agent.id}", headers=headers(agent)).json()
    assert (body["ratings_count"], body["average_rating"], body["csat_percent"]) == (1, 4.0, 100.0)


def test_other_tenant_ratings_never_counted(db, tenant, other_tenant, agent):
    t = make_ticket(db, other_tenant)
    make_rating(
        db,
        session_id=t.session_id,
        tenant_id=other_tenant.id,
        score=5,
        rated_agent_id=agent.id,
        source=RatingSource.AUTO_RESOLVE,
    )
    assert per_agent_rating_stats(db, tenant.id, *_window()) == {}
