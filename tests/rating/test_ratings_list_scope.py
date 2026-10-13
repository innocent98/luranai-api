"""GET /reviews/session: filters, CSAT, enrichment, support-agent own-only scope (§4.3.4)."""

from app.db.models.review import RatingSource
from app.services.rating_request_service import RatingRequestService
from app.services.review_service import ReviewService
from tests.rating.conftest import headers, make_rating, make_ticket

URL = "/api/v1/reviews/session"


def _seed(db, tenant, agent, other_agent):
    """AI 3, agent 5 (tokened, has ticket), agent 4, other_agent 2."""
    t1 = make_ticket(db, tenant, assigned_to=agent.id)
    req = (
        RatingRequestService(db)
        .create_request(t1.id, source=RatingSource.MANUAL_REQUEST, requested_by=agent)
        .request
    )
    make_rating(db, session_id=t1.session_id, tenant_id=tenant.id, score=3)
    make_rating(
        db,
        session_id=t1.session_id,
        tenant_id=tenant.id,
        score=5,
        rated_agent_id=agent.id,
        source=RatingSource.MANUAL_REQUEST,
        rating_request_id=req.id,
    )
    t2 = make_ticket(db, tenant, assigned_to=agent.id)
    make_rating(
        db,
        session_id=t2.session_id,
        tenant_id=tenant.id,
        score=4,
        rated_agent_id=agent.id,
        source=RatingSource.AUTO_RESOLVE,
    )
    t3 = make_ticket(db, tenant, assigned_to=other_agent.id)
    make_rating(
        db,
        session_id=t3.session_id,
        tenant_id=tenant.id,
        score=2,
        rated_agent_id=other_agent.id,
        source=RatingSource.AUTO_RESOLVE,
    )
    return t1


def _get(client, user, **params):
    return client.get(URL, headers=headers(user), params=params)


def test_admin_sees_all_with_enrichment_and_csat(client, db, tenant, agent, other_agent, admin):
    t1 = _seed(db, tenant, agent, other_agent)
    body = _get(client, admin).json()
    assert body["total"] == 4
    assert body["csat_percent"] == 50.0  # 5 and 4 of [3, 5, 4, 2]
    by_score = {r["score"]: r for r in body["ratings"]}
    assert by_score[3]["rated_party"] == "ai" and by_score[3]["rated_agent_name"] is None
    assert by_score[5]["rated_agent_name"] == "Sam Rivers"
    assert by_score[5]["ticket_number"] == t1.ticket_number
    assert by_score[5]["ticket_id"] == str(t1.id)
    assert by_score[4]["ticket_number"] is None  # no request -> no ticket ref


def test_admin_filters(client, db, tenant, agent, other_agent, admin):
    _seed(db, tenant, agent, other_agent)
    assert _get(client, admin, rated_party="ai").json()["total"] == 1
    assert _get(client, admin, rated_party="human").json()["total"] == 3
    assert _get(client, admin, rated_agent_id=str(other_agent.id)).json()["total"] == 1
    assert _get(client, admin, source="auto_resolve").json()["total"] == 2


def test_support_agent_is_scoped_to_own(client, db, tenant, agent, other_agent):
    _seed(db, tenant, agent, other_agent)
    body = _get(client, agent).json()
    assert body["total"] == 2
    assert {r["rated_agent_id"] for r in body["ratings"]} == {str(agent.id)}
    assert body["csat_percent"] == 100.0
    assert _get(client, agent, rated_agent_id=str(agent.id)).json()["total"] == 2


def test_support_agent_other_agent_id_403(client, db, tenant, agent, other_agent):
    _seed(db, tenant, agent, other_agent)
    r = _get(client, agent, rated_agent_id=str(other_agent.id))
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "rating_scope_forbidden"


def test_support_agent_ai_filter_is_empty(client, db, tenant, agent, other_agent):
    _seed(db, tenant, agent, other_agent)
    body = _get(client, agent, rated_party="ai").json()
    assert (body["total"], body["ratings"], body["csat_percent"]) == (0, [], None)


def test_viewer_sees_nothing_of_others(client, db, tenant, agent, other_agent, viewer):
    _seed(db, tenant, agent, other_agent)
    assert _get(client, viewer).json()["total"] == 0


def test_csat_null_when_no_ratings(client, db, tenant, admin):
    body = _get(client, admin).json()
    assert (body["total"], body["average_score"], body["csat_percent"]) == (0, None, None)


def test_legacy_long_comment_still_lists(client, db, tenant, admin):
    t = make_ticket(db, tenant)
    make_rating(db, session_id=t.session_id, tenant_id=tenant.id, comment="y" * 1500)
    r = _get(client, admin)
    assert r.status_code == 200 and r.json()["ratings"][0]["comment"] == "y" * 1500


def test_platform_all_ratings_items_carry_new_fields(db, tenant, agent, other_agent):
    _seed(db, tenant, agent, other_agent)
    out = ReviewService(db).get_all_session_ratings(tenant_id=tenant.id)
    item = next(r for r in out["ratings"] if r.score == 5)
    assert item.rated_party == "human" and item.rated_agent_name == "Sam Rivers"
    assert item.ticket_number is not None


def test_support_agent_cannot_reach_other_agents_ratings_by_any_filter(
    client, db, tenant, agent, other_agent
):
    _seed(db, tenant, agent, other_agent)
    for params in (
        {"rated_agent_id": str(other_agent.id)},
        {"rated_agent_id": str(other_agent.id), "rated_party": "human"},
        {"rated_agent_id": str(other_agent.id), "source": "auto_resolve"},
    ):
        r = _get(client, agent, **params)
        assert r.status_code == 403, params
    body = _get(client, agent, source="auto_resolve").json()
    assert {x["rated_agent_id"] for x in body["ratings"]} == {str(agent.id)}


def test_viewer_other_agent_id_403(client, db, tenant, agent, other_agent, viewer):
    _seed(db, tenant, agent, other_agent)
    assert _get(client, viewer, rated_agent_id=str(agent.id)).status_code == 403


def test_other_tenant_ratings_never_listed(
    client, db, tenant, other_tenant, agent, other_agent, admin
):
    _seed(db, tenant, agent, other_agent)
    t = make_ticket(db, other_tenant)
    make_rating(db, session_id=t.session_id, tenant_id=other_tenant.id, score=1)
    assert _get(client, admin).json()["total"] == 4


def test_list_never_exposes_token_and_decrypts_comment(
    client, db, tenant, agent, other_agent, admin
):
    _seed(db, tenant, agent, other_agent)
    t = make_ticket(db, tenant, assigned_to=agent.id)
    make_rating(
        db,
        session_id=t.session_id,
        tenant_id=tenant.id,
        score=5,
        rated_agent_id=agent.id,
        comment="great help",
    )
    r = _get(client, admin)
    assert "token" not in r.text
    assert "great help" in {x["comment"] for x in r.json()["ratings"]}
