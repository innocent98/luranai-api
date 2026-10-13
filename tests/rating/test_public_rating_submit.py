"""Public POST /reviews/session: legacy path unchanged, token path attributes (spec §4.3.3).

Assertion messages never interpolate a token (tokens are compared via a `leaked` boolean).
"""

from datetime import timedelta
from unittest.mock import AsyncMock, patch

from app.db.models.conversation import ConversationSession
from app.db.models.review import RatingRequest, RatingSource, SessionRating
from app.services.rating_request_service import RatingRequestService
from app.utils.datetime import utcnow_aware
from app.utils.http_rate_limiter import rating_rate_limiter
from tests.rating.conftest import make_ticket

URL = "/api/v1/reviews/session"


def _ident(db, ticket):
    return db.get(ConversationSession, ticket.session_id).user_identifier


def _request(db, ticket, actor):
    return RatingRequestService(db).create_request(
        ticket.id, source=RatingSource.MANUAL_REQUEST, requested_by=actor
    )


def _body(db, ticket, **kw):
    return {
        "session_id": str(ticket.session_id),
        "user_identifier": _ident(db, ticket),
        "score": 5,
        **kw,
    }


def test_legacy_untokened_rating_unchanged(client, db, tenant):
    t = make_ticket(db, tenant)
    r = client.post(URL, json=_body(db, t, meta={"source": "agent_resolved"}))
    assert r.status_code == 201, r.text
    body = r.json()
    assert (body["rated_party"], body["rated_agent_id"], body["source"]) == (
        "ai",
        None,
        "resolution_prompt",
    )
    assert body["rating_request_id"] is None
    dup = client.post(URL, json=_body(db, t))
    assert dup.status_code == 409
    assert dup.json()["detail"] == "Rating already exists for this session"


def test_legacy_default_source_is_idle_prompt(client, db, tenant):
    t = make_ticket(db, tenant)
    assert client.post(URL, json=_body(db, t)).json()["source"] == "idle_prompt"


def test_legacy_string_errors_unchanged(client, db, tenant):
    t = make_ticket(db, tenant)
    missing = client.post(URL, json={**_body(db, t), "session_id": str(tenant.id)})
    assert missing.status_code == 400 and missing.json()["detail"] == "Session not found"
    wrong = client.post(URL, json={**_body(db, t), "user_identifier": "someone-else"})
    assert wrong.status_code == 400
    assert wrong.json()["detail"] == "User identifier does not match session"


def test_token_happy_path_attributes_and_fulfils(client, db, tenant, agent):
    t = make_ticket(db, tenant, assigned_to=agent.id)
    res = _request(db, t, agent)
    r = client.post(URL, json=_body(db, t, rating_request_token=res.token, comment="great"))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["rated_agent_id"] == str(agent.id)
    assert (body["rated_party"], body["source"]) == ("human", "manual_request")
    assert body["rating_request_id"] == str(res.request.id)
    leaked = res.token in r.text
    assert not leaked
    db.expire_all()
    assert db.get(RatingRequest, res.request.id).fulfilled_at is not None


def test_client_sent_rated_agent_id_is_ignored(client, db, tenant, agent, other_agent):
    legacy = make_ticket(db, tenant)
    r1 = client.post(URL, json=_body(db, legacy, rated_agent_id=str(agent.id)))
    assert r1.status_code == 201 and r1.json()["rated_agent_id"] is None
    t = make_ticket(db, tenant, assigned_to=agent.id)
    res = _request(db, t, agent)
    r2 = client.post(
        URL,
        json=_body(db, t, rating_request_token=res.token, rated_agent_id=str(other_agent.id)),
    )
    assert r2.json()["rated_agent_id"] == str(agent.id)


def test_client_sent_source_is_ignored(client, db, tenant, agent):
    t = make_ticket(db, tenant, assigned_to=agent.id)
    res = _request(db, t, agent)
    r = client.post(URL, json=_body(db, t, rating_request_token=res.token, source="auto_resolve"))
    assert r.status_code == 201 and r.json()["source"] == "manual_request"


def test_token_rating_attributes_to_request_agent_after_reassignment(
    client, db, tenant, agent, other_agent
):
    t = make_ticket(db, tenant, assigned_to=agent.id)
    res = _request(db, t, agent)
    t.assigned_to = other_agent.id
    db.commit()
    r = client.post(URL, json=_body(db, t, rating_request_token=res.token))
    assert r.status_code == 201 and r.json()["rated_agent_id"] == str(agent.id)


def test_unknown_token_400(client, db, tenant):
    t = make_ticket(db, tenant)
    r = client.post(URL, json=_body(db, t, rating_request_token="not-a-real-token"))
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "rating_request_invalid"


def test_token_from_another_session_400_indistinguishable_from_unknown(client, db, tenant, agent):
    a = make_ticket(db, tenant, assigned_to=agent.id)
    b = make_ticket(db, tenant, assigned_to=agent.id)
    res_b = _request(db, b, agent)
    req_b_id = res_b.request.id
    cross_body = _body(db, a, rating_request_token=res_b.token)
    unknown_body = _body(db, a, rating_request_token="x" * 43)
    cross = client.post(URL, json=cross_body)
    unknown = client.post(URL, json=unknown_body)
    assert cross.status_code == 400 and cross.json()["detail"]["code"] == "rating_request_invalid"
    assert cross.json() == unknown.json()
    leaked = res_b.token in cross.text
    assert not leaked
    # Nothing was written for either session, and B's request is still open.
    db.expire_all()
    assert db.query(SessionRating).count() == 0
    assert db.get(RatingRequest, req_b_id).fulfilled_at is None


def test_reused_token_409_object(client, db, tenant, agent):
    t = make_ticket(db, tenant, assigned_to=agent.id)
    res = _request(db, t, agent)
    body, sid = _body(db, t, rating_request_token=res.token), t.session_id
    first = client.post(URL, json=body)
    assert first.status_code == 201
    again = client.post(URL, json=body)
    assert again.status_code == 409 and again.json()["detail"]["code"] == "already_rated"
    db.expire_all()
    assert db.query(SessionRating).filter_by(session_id=sid).count() == 1


def test_expired_token_410(client, db, tenant, agent):
    t = make_ticket(db, tenant, assigned_to=agent.id)
    res = _request(db, t, agent)
    res.request.expires_at = utcnow_aware() - timedelta(seconds=1)
    db.commit()
    r = client.post(URL, json=_body(db, t, rating_request_token=res.token))
    assert r.status_code == 410 and r.json()["detail"]["code"] == "rating_request_expired"
    db.expire_all()
    assert db.query(SessionRating).count() == 0


def test_superseded_token_410_new_token_201(client, db, tenant, agent):
    t = make_ticket(db, tenant, assigned_to=agent.id)
    old = _request(db, t, agent)
    old.request.created_at = utcnow_aware() - timedelta(minutes=11)
    db.commit()
    new = _request(db, t, agent)
    stale_body = _body(db, t, rating_request_token=old.token)
    fresh_body = _body(db, t, rating_request_token=new.token)
    stale = client.post(URL, json=stale_body)
    assert stale.status_code == 410
    fresh = client.post(URL, json=fresh_body)
    assert fresh.status_code == 201


def test_legacy_resolution_rating_and_human_rating_coexist(client, db, tenant, agent):
    t = make_ticket(db, tenant, assigned_to=agent.id)
    res = _request(db, t, agent)
    old_widget = client.post(URL, json=_body(db, t, meta={"source": "agent_resolved"}))
    assert old_widget.status_code == 201 and old_widget.json()["rated_party"] == "ai"
    human = client.post(URL, json=_body(db, t, rating_request_token=res.token))
    assert human.status_code == 201 and human.json()["rated_party"] == "human"


def test_identifier_mismatch_with_valid_token_is_string_400_and_keeps_token(
    client, db, tenant, agent
):
    t = make_ticket(db, tenant, assigned_to=agent.id)
    res = _request(db, t, agent)
    r = client.post(
        URL, json={**_body(db, t, rating_request_token=res.token), "user_identifier": "intruder"}
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "User identifier does not match session"
    db.expire_all()
    assert db.get(RatingRequest, res.request.id).fulfilled_at is None


def test_token_length_bounds_422(client, db, tenant):
    t = make_ticket(db, tenant)
    assert client.post(URL, json=_body(db, t, rating_request_token="")).status_code == 422
    assert client.post(URL, json=_body(db, t, rating_request_token="x" * 129)).status_code == 422


def test_comment_over_500_is_422(client, db, tenant):
    t = make_ticket(db, tenant)
    assert client.post(URL, json=_body(db, t, comment="x" * 501)).status_code == 422
    assert client.post(URL, json=_body(db, t, comment="x" * 500)).status_code == 201


def test_rate_limited_429_object(client, db, tenant):
    t = make_ticket(db, tenant)
    with patch.object(
        rating_rate_limiter, "get_current_usage", AsyncMock(return_value={"calls": 20})
    ):
        r = client.post(URL, json=_body(db, t))
    assert r.status_code == 429
    assert r.json()["detail"]["code"] == "rating_rate_limited"


def test_foreign_token_does_not_lock_or_touch_other_session(client, db, tenant, agent):
    """The locking query is scoped to the posted session (cross-session token matches no row)."""
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.sql import Select

    captured: list[Select] = []
    real_execute = db.execute

    def spy(stmt, *a, **kw):
        captured.append(stmt)
        return real_execute(stmt, *a, **kw)

    a = make_ticket(db, tenant, assigned_to=agent.id)
    b = make_ticket(db, tenant, assigned_to=agent.id)
    res_b = _request(db, b, agent)
    body = _body(db, a, rating_request_token=res_b.token)
    db.execute = spy
    try:
        r = client.post(URL, json=body)
    finally:
        db.execute = real_execute
    assert r.status_code == 400
    locking = [
        s for s in captured if "rating_requests" in str(s.compile(dialect=postgresql.dialect()))
    ]
    assert locking, "expected a rating_requests lookup"
    sql = str(locking[0].compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE" in sql and "rating_requests.session_id" in sql
