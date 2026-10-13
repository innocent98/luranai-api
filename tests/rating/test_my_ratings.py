"""GET /agent/me/ratings: own only, no customer identity, newest 20 (§4.3.5)."""

from datetime import timedelta

from app.db.models.review import RatingSource
from app.services.rating_request_service import RatingRequestService
from app.utils.datetime import utcnow_naive
from tests.rating.conftest import headers, make_rating, make_ticket

URL = "/api/v1/agent/me/ratings"


def test_returns_only_own_with_stats_and_ticket_number(client, db, tenant, agent, other_agent):
    t = make_ticket(db, tenant, assigned_to=agent.id)
    req = (
        RatingRequestService(db)
        .create_request(t.id, source=RatingSource.MANUAL_REQUEST, requested_by=agent)
        .request
    )
    make_rating(
        db,
        session_id=t.session_id,
        tenant_id=tenant.id,
        score=5,
        comment="kind",
        rated_agent_id=agent.id,
        source=RatingSource.MANUAL_REQUEST,
        rating_request_id=req.id,
    )
    t2 = make_ticket(db, tenant, assigned_to=other_agent.id)
    make_rating(
        db, session_id=t2.session_id, tenant_id=tenant.id, score=1, rated_agent_id=other_agent.id
    )
    body = client.get(URL, headers=headers(agent)).json()
    assert (body["count"], body["average_score"], body["csat_percent"]) == (1, 5.0, 100.0)
    assert len(body["recent"]) == 1
    item = body["recent"][0]
    assert set(item) == {"id", "score", "comment", "created_at", "ticket_number", "source"}
    assert (item["ticket_number"], item["comment"]) == (t.ticket_number, "kind")
    assert set(body) == {
        "period_start",
        "period_end",
        "count",
        "average_score",
        "csat_percent",
        "recent",
    }


def test_recent_is_newest_20_and_stats_cover_all(client, db, tenant, agent):
    base = utcnow_naive() - timedelta(days=1)
    for i in range(21):
        t = make_ticket(db, tenant, assigned_to=agent.id)
        make_rating(
            db,
            session_id=t.session_id,
            tenant_id=tenant.id,
            score=4,
            rated_agent_id=agent.id,
            source=RatingSource.AUTO_RESOLVE,
            created_at=base + timedelta(minutes=i),
        )
    body = client.get(URL, headers=headers(agent)).json()
    assert body["count"] == 21 and len(body["recent"]) == 20
    stamps = [r["created_at"] for r in body["recent"]]
    assert stamps == sorted(stamps, reverse=True)


def test_empty_state_is_nulls(client, db, tenant, agent):
    body = client.get(URL, headers=headers(agent)).json()
    assert (body["count"], body["average_score"], body["csat_percent"], body["recent"]) == (
        0,
        None,
        None,
        [],
    )


def test_viewer_forbidden(client, db, tenant, viewer):
    assert client.get(URL, headers=headers(viewer)).status_code == 403


def test_end_before_start_400(client, db, tenant, agent):
    r = client.get(
        URL,
        headers=headers(agent),
        params={"start_date": "2026-10-10T00:00:00Z", "end_date": "2026-10-01T00:00:00Z"},
    )
    assert r.status_code == 400 and r.json()["detail"] == "End date must be after start date"


def test_other_tenant_rating_for_same_agent_id_excluded(client, db, tenant, other_tenant, agent):
    t = make_ticket(db, other_tenant)
    make_rating(
        db,
        session_id=t.session_id,
        tenant_id=other_tenant.id,
        score=5,
        rated_agent_id=agent.id,
        source=RatingSource.AUTO_RESOLVE,
    )
    assert client.get(URL, headers=headers(agent)).json()["count"] == 0
