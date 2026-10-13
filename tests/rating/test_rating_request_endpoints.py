"""POST /tickets/{id}/rating-requests and GET /tickets/{id}/rating-status (spec §4.3.1-2)."""

from datetime import timedelta
from uuid import UUID

import pytest

from app.db.models.conversation import Message, TicketStatus
from app.db.models.review import RatingRequest, RatingSource
from app.services.rating_request_service import RatingRequestService, hash_token
from app.utils.datetime import utcnow_aware
from tests.rating.conftest import headers, make_rating, make_ticket

KEYS_201 = {
    "request_id",
    "ticket_id",
    "session_id",
    "agent_id",
    "agent_name",
    "source",
    "message_id",
    "created_at",
    "expires_at",
}


def _post(client, ticket, user):
    return client.post(f"/api/v1/tickets/{ticket.id}/rating-requests", headers=headers(user))


def _status(client, ticket, user):
    return client.get(f"/api/v1/tickets/{ticket.id}/rating-status", headers=headers(user))


def test_assignee_request_201_and_frames(client, db, tenant, agent, fanout):
    t = make_ticket(db, tenant, assigned_to=agent.id)
    r = _post(client, t, agent)
    assert r.status_code == 201, r.text
    body = r.json()
    assert set(body) == KEYS_201
    assert (body["agent_name"], body["source"]) == ("Sam Rivers", "manual_request")
    assert "token" not in r.text

    calls = [c.kwargs for c in fanout.await_args_list]
    assert [c["message"]["type"] for c in calls] == ["rating_requested", "rating_request_created"]
    customer, agents = calls
    assert (customer["to_customer"], customer["to_agents"]) == (True, False)
    assert (agents["to_customer"], agents["to_agents"]) == (False, True)
    frame = customer["message"]
    assert set(frame) == {
        "type",
        "request_id",
        "token",
        "agent_name",
        "message_id",
        "content",
        "source",
        "timestamp",
        "expires_at",
    }
    assert frame["agent_name"] == "Sam" and frame["content"] == "How was your chat with Sam?"
    req = db.get(RatingRequest, UUID(body["request_id"]))
    assert hash_token(frame["token"]) == req.token_hash
    assert set(agents["message"]) == {
        "type",
        "request_id",
        "ticket_id",
        "agent_id",
        "source",
        "created_at",
    }


def test_admin_may_request_for_assignee(client, db, tenant, agent, admin):
    t = make_ticket(db, tenant, assigned_to=agent.id)
    r = _post(client, t, admin)
    assert r.status_code == 201 and r.json()["agent_id"] == str(agent.id)


@pytest.mark.parametrize("who", ["other_agent", "viewer"])
def test_non_assignee_and_viewer_403(client, db, tenant, agent, request, who):
    t = make_ticket(db, tenant, assigned_to=agent.id)
    r = _post(client, t, request.getfixturevalue(who))
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "rating_request_forbidden"


def test_other_tenant_ticket_is_404_string(client, db, other_tenant, agent):
    t = make_ticket(db, other_tenant)
    for r in (_post(client, t, agent), _status(client, t, agent)):
        assert r.status_code == 404 and r.json()["detail"] == "Ticket not found"


def test_unassigned_409_no_human_agent(client, db, tenant, admin):
    t = make_ticket(db, tenant, status=TicketStatus.OPEN)
    r = _post(client, t, admin)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "no_human_agent"


def test_open_ticket_409_not_ratable(client, db, tenant, agent):
    t = make_ticket(db, tenant, status=TicketStatus.OPEN, assigned_to=agent.id)
    r = _post(client, t, agent)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "ticket_not_ratable"


def test_second_request_inside_10_min_409_cooldown(client, db, tenant, agent):
    t = make_ticket(db, tenant, assigned_to=agent.id)
    assert _post(client, t, agent).status_code == 201
    r = _post(client, t, agent)
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "rating_request_cooldown"
    assert 0 < detail["retry_after_seconds"] <= 600


def test_already_rated_409(client, db, tenant, agent):
    t = make_ticket(db, tenant, assigned_to=agent.id)
    make_rating(db, session_id=t.session_id, tenant_id=tenant.id, rated_agent_id=agent.id)
    r = _post(client, t, agent)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "already_rated"


def test_status_can_request_then_cooldown_with_open_request(client, db, tenant, agent):
    t = make_ticket(db, tenant, assigned_to=agent.id)
    first = _status(client, t, agent).json()
    assert first == {
        "can_request": True,
        "reason_code": None,
        "retry_after_seconds": None,
        "open_request": None,
        "rating": None,
    }
    req_id = _post(client, t, agent).json()["request_id"]
    after = _status(client, t, agent)
    assert "token" not in after.text
    body = after.json()
    assert (body["can_request"], body["reason_code"]) == (False, "rating_request_cooldown")
    assert body["open_request"]["request_id"] == req_id
    assert body["open_request"]["source"] == "manual_request"


def test_status_rating_visible_only_to_admin_and_rated_agent(
    client, db, tenant, agent, other_agent, admin
):
    t = make_ticket(db, tenant, assigned_to=agent.id)
    make_rating(
        db,
        session_id=t.session_id,
        tenant_id=tenant.id,
        rated_agent_id=agent.id,
        score=2,
        comment="slow",
        source=RatingSource.MANUAL_REQUEST,
    )
    for user in (admin, agent):
        rating = _status(client, t, user).json()["rating"]
        assert rating["score"] == 2 and rating["comment"] == "slow"
        assert rating["rated_agent_name"] == "Sam Rivers" and rating["rated_party"] == "human"
    hidden = _status(client, t, other_agent).json()
    assert hidden["rating"] is None
    assert hidden["reason_code"] == "rating_request_forbidden"


def test_status_viewer_403(client, db, tenant, agent, viewer):
    t = make_ticket(db, tenant, assigned_to=agent.id)
    r = _status(client, t, viewer)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "rating_request_forbidden"


def test_fanout_failure_does_not_fail_request(client, db, tenant, agent, fanout):
    fanout.side_effect = RuntimeError("redis down")
    t = make_ticket(db, tenant, assigned_to=agent.id)
    assert _post(client, t, agent).status_code == 201


def test_supersede_via_endpoint_after_cooldown(client, db, tenant, agent):
    t = make_ticket(db, tenant, assigned_to=agent.id)
    old = (
        RatingRequestService(db)
        .create_request(t.id, source=RatingSource.MANUAL_REQUEST, requested_by=agent)
        .request
    )
    old.created_at = utcnow_aware() - timedelta(minutes=11)
    db.commit()
    r = _post(client, t, agent)
    assert r.status_code == 201 and r.json()["request_id"] != str(old.id)


def test_failed_request_releases_row_lock(client, db, tenant, agent):
    """A refused request ends its transaction so the ticket FOR UPDATE lock is not held."""
    from unittest.mock import patch

    t = make_ticket(db, tenant, status=TicketStatus.OPEN, assigned_to=agent.id)
    with patch.object(db, "commit", wraps=db.commit) as ended:
        r = _post(client, t, agent)
    assert r.status_code == 409
    assert ended.called  # transaction ended on refusal -> row lock released


def test_token_never_logged(client, db, tenant, agent, fanout, caplog):
    fanout.side_effect = RuntimeError("redis down")
    t = make_ticket(db, tenant, assigned_to=agent.id)
    with caplog.at_level("DEBUG"):
        assert _post(client, t, agent).status_code == 201
    msg = db.query(Message).filter_by(session_id=t.session_id).one()
    token = msg.meta["rating_token"]
    leaked = token in caplog.text
    assert not leaked, "rating token leaked into logs"


def test_manual_request_fans_out_only_after_commit(client, db, tenant, agent, fanout):
    events: list[str] = []
    real_commit = db.commit

    def spy_commit():
        events.append("commit")
        return real_commit()

    async def spy_fanout(**kwargs):
        events.append("fanout")

    fanout.side_effect = spy_fanout
    t = make_ticket(db, tenant, assigned_to=agent.id)
    db.commit = spy_commit
    try:
        r = _post(client, t, agent)
    finally:
        db.commit = real_commit
    assert r.status_code == 201, r.text
    assert "fanout" in events and "commit" in events
    assert events.index("fanout") > len(events) - 1 - events[::-1].index("commit")


def test_locking_handlers_do_not_run_on_the_event_loop():
    import inspect

    from app.api.v1.endpoints import reviews, ticket_ratings

    assert not inspect.iscoroutinefunction(reviews.create_session_rating)
    assert not inspect.iscoroutinefunction(ticket_ratings.rating_status)
