"""Spec §4.6: the rating token never reaches a staff-facing surface; customers keep it."""

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.api.deps import get_current_customer
from app.db.models.conversation import ConversationSession, Message, MessageRole
from app.db.models.review import RatingSource
from app.main import app
from app.schemas.customer_auth import CustomerUserResponse
from app.services.rating_request_service import RatingRequestService
from app.utils.datetime import utcnow_naive
from app.utils.staff_redaction import redact_staff_meta
from tests.api.v1.test_agent_websocket_auth import agent_ws_env  # noqa: F401 (fixture)
from tests.rating.conftest import headers, make_rating, make_ticket


def _requested(db, tenant, agent):
    t = make_ticket(db, tenant, assigned_to=agent.id)
    res = RatingRequestService(db).create_request(
        t.id, source=RatingSource.MANUAL_REQUEST, requested_by=agent
    )
    return t, res.token, res.request


def test_redact_staff_meta_unit():
    assert redact_staff_meta(None) is None
    assert redact_staff_meta({}) == {}
    plain = {"system_notice": "ticket_missed"}
    assert redact_staff_meta(plain) is plain
    src = {"system_notice": "rating_request", "request_id": "r", "rating_token": "t"}
    assert redact_staff_meta(src) == {"system_notice": "rating_request", "request_id": "r"}
    assert "rating_token" in src  # input not mutated


def test_admin_session_detail_and_message_list_have_no_token(client, db, tenant, agent, admin):
    t, token, req = _requested(db, tenant, agent)
    for path in (
        f"/api/v1/chat/sessions/{t.session_id}",
        f"/api/v1/chat/sessions/{t.session_id}/messages",
    ):
        r = client.get(path, headers=headers(admin))
        assert r.status_code == 200, r.text
        assert "rating_token" not in r.text
        assert token not in r.text
        assert str(req.id) in r.text  # the notice itself is still shown to staff


def test_agent_app_escalation_detail_has_no_token(client, db, tenant, agent):
    t, token, _ = _requested(db, tenant, agent)
    r = client.get(f"/api/v1/agent/escalations/{t.id}", headers=headers(agent))
    assert r.status_code == 200, r.text
    assert token not in r.text and "rating_token" not in r.text


def test_agent_app_escalation_list_has_no_token(client, db, tenant, agent):
    t, token, _ = _requested(db, tenant, agent)
    r = client.get("/api/v1/agent/escalations", headers=headers(agent))
    assert r.status_code == 200, r.text
    assert str(t.id) in r.text
    assert token not in r.text and "rating_token" not in r.text


def test_customer_history_keeps_token(client, db, tenant, agent):
    t, token, req = _requested(db, tenant, agent)
    session = db.get(ConversationSession, t.session_id)
    now = datetime.now(UTC)
    customer = CustomerUserResponse(
        id=uuid4(),
        full_name="Cus Tomer",
        email="customer@example.com",
        chat_user_key=session.user_identifier,
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    app.dependency_overrides[get_current_customer] = lambda: customer
    try:
        r = client.get(f"/api/v1/customer/conversations/{t.session_id}/messages")
    finally:
        app.dependency_overrides.pop(get_current_customer, None)
    assert r.status_code == 200, r.text
    notices = [
        m
        for m in r.json()["messages"]
        if (m.get("meta") or {}).get("system_notice") == "rating_request"
    ]
    assert notices and notices[0]["meta"]["rating_token"] == token
    assert notices[0]["meta"]["request_id"] == str(req.id)


def test_session_detail_lists_every_rating_oldest_first(client, db, tenant, agent, admin):
    t = make_ticket(db, tenant, assigned_to=agent.id)
    make_rating(
        db,
        session_id=t.session_id,
        tenant_id=tenant.id,
        score=3,
        created_at=utcnow_naive() - timedelta(hours=2),
    )
    make_rating(
        db,
        session_id=t.session_id,
        tenant_id=tenant.id,
        score=5,
        rated_agent_id=agent.id,
        source=RatingSource.MANUAL_REQUEST,
        comment="lovely",
        created_at=utcnow_naive() - timedelta(hours=1),
    )
    body = client.get(f"/api/v1/chat/sessions/{t.session_id}", headers=headers(admin)).json()
    assert [(r["rated_party"], r["score"]) for r in body["ratings"]] == [("ai", 3), ("human", 5)]
    human = body["ratings"][1]
    assert human["rated_agent_name"] == "Sam Rivers"
    assert human["source"] == "manual_request" and human["comment"] == "lovely"
    assert set(human) == {
        "id",
        "score",
        "comment",
        "created_at",
        "rated_party",
        "rated_agent_id",
        "rated_agent_name",
        "source",
    }


def test_agent_ws_replay_never_carries_token(agent_ws_env):  # noqa: F811
    s = agent_ws_env.session_factory()
    try:
        s.add(
            Message(
                session_id=agent_ws_env.session_id,
                role=MessageRole.ASSISTANT,
                content="How was your chat with Sam?",
                meta={
                    "system_notice": "rating_request",
                    "request_id": "r1",
                    "agent_first_name": "Sam",
                    "rating_token": "tok-secret-value",
                },
            )
        )
        s.add(Message(session_id=agent_ws_env.session_id, role=MessageRole.USER, content="hi"))
        s.commit()
    finally:
        s.close()
    frames = []
    url = f"{agent_ws_env.url}?token={agent_ws_env.valid_token()}"
    with agent_ws_env.client.websocket_connect(url) as ws:
        ws.send_json({"type": "ping"})
        while True:
            frame = ws.receive_json()
            frames.append(frame)
            if frame.get("type") == "pong":
                break
    dumped = json.dumps(frames)
    assert "tok-secret-value" not in dumped and "rating_token" not in dumped
    assert any(f.get("type") == "customer_message" for f in frames)
