"""RatingRequestService (spec §4.2, N4-D2/D3)."""

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.db.models.conversation import Message, MessageRole, TicketStatus
from app.db.models.review import RatingRequest, RatingSource
from app.services.rating_request_service import (
    REQUEST_TTL,
    RatingRequestError,
    RatingRequestService,
    first_name,
    hash_token,
    prompt_copy,
)
from app.utils.datetime import ensure_utc_aware, utcnow_aware, utcnow_naive
from tests.rating.conftest import make_rating, make_ticket

MANUAL = RatingSource.MANUAL_REQUEST
AUTO = RatingSource.AUTO_RESOLVE


def _svc(db):
    return RatingRequestService(db)


def _age(db, req, delta):
    req.created_at = utcnow_aware() - delta
    db.commit()


def _messages(db, session_id):
    return db.execute(
        select(func.count(Message.id)).where(Message.session_id == session_id)
    ).scalar_one()


def _code(exc_info):
    return exc_info.value.code, exc_info.value.status_code


def test_first_name_and_copy():
    assert first_name("Sam Rivers") == "Sam"
    assert first_name("  ") is None
    assert first_name(None) is None
    assert prompt_copy("Sam") == "How was your chat with Sam?"
    assert prompt_copy(None) == "How was your chat with our team?"


def test_manual_request_persists_request_and_bound_message(db, tenant, agent):
    t = make_ticket(db, tenant, assigned_to=agent.id)
    res = _svc(db).create_request(t.id, source=MANUAL, requested_by=agent)
    req = res.request
    assert res.created is True and res.token
    assert req.token_hash == hash_token(res.token) and len(req.token_hash) == 64
    assert (req.agent_id, req.requested_by, req.source, req.ticket_id) == (
        agent.id,
        agent.id,
        MANUAL,
        t.id,
    )
    assert ensure_utc_aware(req.expires_at) - ensure_utc_aware(req.created_at) == REQUEST_TTL
    assert req.fulfilled_at is None
    msg = db.get(Message, req.message_id)
    assert msg.role == MessageRole.ASSISTANT
    assert msg.content == "How was your chat with Sam?"
    assert msg.meta == {
        "system_notice": "rating_request",
        "request_id": str(req.id),
        "agent_first_name": "Sam",
        "rating_token": res.token,
    }
    assert (res.agent_first_name, res.agent_name) == ("Sam", "Sam Rivers")


def test_blank_agent_name_uses_team_copy(db, tenant, agent):
    agent.full_name = " "
    db.commit()
    t = make_ticket(db, tenant, assigned_to=agent.id)
    res = _svc(db).create_request(t.id, source=MANUAL, requested_by=agent)
    assert res.message.content == "How was your chat with our team?"
    assert res.message.meta["agent_first_name"] is None


def test_no_human_agent(db, tenant, admin):
    t = make_ticket(db, tenant, assigned_to=None)
    with pytest.raises(RatingRequestError) as e:
        _svc(db).create_request(t.id, source=MANUAL, requested_by=admin)
    assert _code(e) == ("no_human_agent", 409)


def test_non_assignee_agent_forbidden_admin_allowed(db, tenant, agent, other_agent, admin):
    t = make_ticket(db, tenant, assigned_to=agent.id)
    with pytest.raises(RatingRequestError) as e:
        _svc(db).create_request(t.id, source=MANUAL, requested_by=other_agent)
    assert _code(e) == ("rating_request_forbidden", 403)
    res = _svc(db).create_request(t.id, source=MANUAL, requested_by=admin)
    assert res.request.agent_id == agent.id and res.request.requested_by == admin.id


@pytest.mark.parametrize("status", [TicketStatus.OPEN, TicketStatus.MISSED, TicketStatus.CLOSED])
def test_not_ratable_statuses(db, tenant, agent, status):
    t = make_ticket(db, tenant, status=status, assigned_to=agent.id)
    with pytest.raises(RatingRequestError) as e:
        _svc(db).create_request(t.id, source=MANUAL, requested_by=agent)
    assert _code(e) == ("ticket_not_ratable", 409)


def test_resolved_ratable_for_24_hours_only(db, tenant, agent):
    fresh = make_ticket(
        db,
        tenant,
        status=TicketStatus.RESOLVED,
        assigned_to=agent.id,
        resolved_at=utcnow_naive() - timedelta(hours=23),
    )
    stale = make_ticket(
        db,
        tenant,
        status=TicketStatus.RESOLVED,
        assigned_to=agent.id,
        resolved_at=utcnow_naive() - timedelta(hours=25),
    )
    assert _svc(db).create_request(fresh.id, source=MANUAL, requested_by=agent).created
    with pytest.raises(RatingRequestError) as e:
        _svc(db).create_request(stale.id, source=MANUAL, requested_by=agent)
    assert _code(e) == ("ticket_not_ratable", 409)


def test_already_rated_blocks_manual_and_auto(db, tenant, agent):
    t = make_ticket(db, tenant, assigned_to=agent.id)
    make_rating(db, session_id=t.session_id, tenant_id=tenant.id, rated_agent_id=agent.id)
    for source in (MANUAL, AUTO):
        with pytest.raises(RatingRequestError) as e:
            _svc(db).create_request(t.id, source=source, requested_by=agent)
        assert _code(e) == ("already_rated", 409)


def test_ai_rating_does_not_count_as_agent_rated(db, tenant, agent):
    t = make_ticket(db, tenant, assigned_to=agent.id)
    make_rating(db, session_id=t.session_id, tenant_id=tenant.id)  # AI rating
    assert _svc(db).create_request(t.id, source=MANUAL, requested_by=agent).created


def test_cooldown_reports_retry_after(db, tenant, agent):
    t = make_ticket(db, tenant, assigned_to=agent.id)
    first = _svc(db).create_request(t.id, source=MANUAL, requested_by=agent)
    _age(db, first.request, timedelta(minutes=9))
    with pytest.raises(RatingRequestError) as e:
        _svc(db).create_request(t.id, source=MANUAL, requested_by=agent)
    assert _code(e) == ("rating_request_cooldown", 409)
    assert 55 <= e.value.retry_after_seconds <= 61
    assert e.value.detail()["retry_after_seconds"] == e.value.retry_after_seconds


def test_manual_after_cooldown_supersedes_open_request(db, tenant, agent):
    t = make_ticket(db, tenant, assigned_to=agent.id)
    first = _svc(db).create_request(t.id, source=MANUAL, requested_by=agent)
    _age(db, first.request, timedelta(minutes=10, seconds=1))
    second = _svc(db).create_request(t.id, source=MANUAL, requested_by=agent)
    db.refresh(first.request)
    assert second.request.id != first.request.id
    assert ensure_utc_aware(first.request.expires_at) <= utcnow_aware()
    assert _svc(db).open_request(t.session_id, agent.id).id == second.request.id
    assert _messages(db, t.session_id) == 2


def test_auto_reuses_open_request_without_new_message(db, tenant, agent):
    t = make_ticket(db, tenant, assigned_to=agent.id)
    manual = _svc(db).create_request(t.id, source=MANUAL, requested_by=agent)
    auto = _svc(db).create_request(t.id, source=AUTO, requested_by=None)
    assert auto.created is False and auto.token is None and auto.message is None
    assert auto.request.id == manual.request.id
    assert _messages(db, t.session_id) == 1


def test_auto_skips_cooldown_when_nothing_open(db, tenant, agent):
    t = make_ticket(db, tenant, assigned_to=agent.id)
    manual = _svc(db).create_request(t.id, source=MANUAL, requested_by=agent)
    manual.request.expires_at = utcnow_aware()  # expired a moment ago, created < 10 min ago
    db.commit()
    auto = _svc(db).create_request(t.id, source=AUTO, requested_by=None)
    assert auto.created is True and auto.request.source == AUTO
    assert auto.request.requested_by is None


def test_rating_state(db, tenant, agent):
    svc = _svc(db)
    unassigned = make_ticket(db, tenant)
    assert svc.rating_state(unassigned) == ("none", None)
    t = make_ticket(db, tenant, assigned_to=agent.id)
    assert svc.rating_state(t) == ("none", None)
    req = svc.create_request(t.id, source=MANUAL, requested_by=agent).request
    assert svc.rating_state(t) == ("pending", req.id)
    make_rating(db, session_id=t.session_id, tenant_id=tenant.id, rated_agent_id=agent.id)
    assert svc.rating_state(t) == ("already_rated", None)


def test_evaluate_reports_reason_without_raising(db, tenant, agent, other_agent):
    t = make_ticket(db, tenant, assigned_to=agent.id)
    ok = _svc(db).evaluate(t, agent)
    assert (ok.can_request, ok.reason_code, ok.open_request) == (True, None, None)
    no = _svc(db).evaluate(t, other_agent)
    assert (no.can_request, no.reason_code) == (False, "rating_request_forbidden")
    req = _svc(db).create_request(t.id, source=MANUAL, requested_by=agent).request
    cool = _svc(db).evaluate(t, agent)
    assert cool.reason_code == "rating_request_cooldown" and cool.open_request.id == req.id
    assert db.execute(select(func.count(RatingRequest.id))).scalar_one() >= 1
