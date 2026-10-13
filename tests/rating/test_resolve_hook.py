"""Resolve hook (spec §4.4): request before ticket_resolved; close carries fields."""

import inspect
import logging
from unittest.mock import patch

from sqlalchemy import func, select

import app.workers.conversation_auto_resolver as auto_resolver
from app.db.models.conversation import Message, Ticket, TicketStatus
from app.db.models.review import RatingRequest, RatingSource
from app.services.rating_request_service import RatingRequestService
from tests.rating.conftest import headers, make_rating, make_ticket


def _resolve(client, ticket, user):
    return client.post(
        f"/api/v1/tickets/{ticket.id}/resolve",
        json={"resolution_notes": "Fixed"},
        headers=headers(user),
    )


def _types(fanout):
    return [c.kwargs["message"]["type"] for c in fanout.await_args_list]


def _requests(db, ticket):
    return db.execute(
        select(func.count(RatingRequest.id)).where(RatingRequest.session_id == ticket.session_id)
    ).scalar_one()


def test_resolve_with_assignee_requests_rating_before_ticket_resolved(
    client, db, tenant, agent, fanout
):
    t = make_ticket(db, tenant, assigned_to=agent.id)
    r = _resolve(client, t, agent)
    assert r.status_code == 200, r.text
    assert _types(fanout) == ["rating_requested", "rating_request_created", "ticket_resolved"]
    resolved = fanout.await_args_list[-1].kwargs["message"]
    req = db.execute(select(RatingRequest).where(RatingRequest.ticket_id == t.id)).scalar_one()
    assert (req.source, req.requested_by) == (RatingSource.AUTO_RESOLVE, None)
    assert resolved["rating_state"] == "pending"
    assert resolved["rating_request_id"] == str(req.id)
    assert r.json()["rating_request_id"] == str(req.id)
    assert "token" not in resolved


def test_resolve_reuses_open_manual_request_without_new_card(client, db, tenant, agent, fanout):
    t = make_ticket(db, tenant, assigned_to=agent.id)
    manual = RatingRequestService(db).create_request(
        t.id, source=RatingSource.MANUAL_REQUEST, requested_by=agent
    )
    fanout.reset_mock()
    r = _resolve(client, t, agent)
    assert r.status_code == 200
    assert _types(fanout) == ["ticket_resolved"]
    resolved = fanout.await_args_list[-1].kwargs["message"]
    assert (resolved["rating_state"], resolved["rating_request_id"]) == (
        "pending",
        str(manual.request.id),
    )
    assert _requests(db, t) == 1


def test_admin_resolving_unassigned_missed_ticket_is_none(client, db, tenant, admin, fanout):
    t = make_ticket(db, tenant, status=TicketStatus.MISSED, assigned_to=None)
    r = _resolve(client, t, admin)
    assert r.status_code == 200
    assert _types(fanout) == ["ticket_resolved"]
    resolved = fanout.await_args_list[-1].kwargs["message"]
    assert (resolved["rating_state"], resolved["rating_request_id"]) == ("none", None)
    assert r.json()["rating_request_id"] is None
    assert _requests(db, t) == 0


def test_resolve_when_agent_already_rated(client, db, tenant, agent, fanout):
    t = make_ticket(db, tenant, assigned_to=agent.id)
    make_rating(db, session_id=t.session_id, tenant_id=tenant.id, rated_agent_id=agent.id)
    r = _resolve(client, t, agent)
    assert r.status_code == 200
    resolved = fanout.await_args_list[-1].kwargs["message"]
    assert (resolved["rating_state"], resolved["rating_request_id"]) == ("already_rated", None)
    assert _requests(db, t) == 0


def test_request_failure_never_fails_resolve(client, db, tenant, agent, fanout):
    t = make_ticket(db, tenant, assigned_to=agent.id)
    with patch(
        "app.services.rating_request_service.RatingRequestService.create_request",
        side_effect=RuntimeError("db hiccup"),
    ):
        r = _resolve(client, t, agent)
    assert r.status_code == 200, r.text
    assert db.get(Ticket, t.id).status == TicketStatus.RESOLVED
    assert _types(fanout) == ["ticket_resolved"]
    assert fanout.await_args_list[-1].kwargs["message"]["rating_state"] == "none"


def test_close_carries_rating_fields_without_creating(client, db, tenant, agent, admin, fanout):
    t = make_ticket(db, tenant, assigned_to=agent.id)
    req = (
        RatingRequestService(db)
        .create_request(t.id, source=RatingSource.MANUAL_REQUEST, requested_by=agent)
        .request
    )
    fanout.reset_mock()
    r = client.post(f"/api/v1/tickets/{t.id}/close", headers=headers(admin))
    assert r.status_code == 200
    closed = fanout.await_args_list[-1].kwargs["message"]
    assert closed["type"] == "ticket_closed"
    assert "token" not in closed
    assert (closed["rating_state"], closed["rating_request_id"]) == ("pending", str(req.id))
    assert _requests(db, t) == 1


def test_close_unassigned_is_none(client, db, tenant, admin, fanout):
    t = make_ticket(db, tenant, status=TicketStatus.OPEN)
    client.post(f"/api/v1/tickets/{t.id}/close", headers=headers(admin))
    closed = fanout.await_args_list[-1].kwargs["message"]
    assert (closed["rating_state"], closed["rating_request_id"]) == ("none", None)


def test_auto_resolver_never_requests_ratings():
    assert "rating_request" not in inspect.getsource(auto_resolver)


def test_frame_build_failure_is_logged_token_free_and_resolve_still_fans_out(
    client, db, tenant, agent, fanout, caplog, monkeypatch
):
    """Frame building runs inside the per-frame try (Task 4 follow-up): it cannot fail resolve."""
    # Alembic's env.py runs logging.config.fileConfig() (disable_existing_loggers=True) whenever a
    # migration test runs earlier in the suite, which silently disables already-imported loggers
    # and leaves caplog empty. Re-enable the exact logger so this test is order-independent.
    monkeypatch.setattr(logging.getLogger("app.services.rating_request_service"), "disabled", False)
    t = make_ticket(db, tenant, assigned_to=agent.id)
    with (
        patch(
            "app.services.rating_request_service.customer_frame", side_effect=RuntimeError("boom")
        ),
        caplog.at_level("DEBUG"),
    ):
        r = _resolve(client, t, agent)
    assert r.status_code == 200, r.text
    assert _types(fanout) == ["rating_request_created", "ticket_resolved"]
    assert "rating request fanout failed" in caplog.text
    token = db.query(Message).filter_by(session_id=t.session_id).one().meta["rating_token"]
    leaked = token in caplog.text
    assert not leaked, "rating token leaked into logs"


def test_auto_request_locking_select_runs_off_the_event_loop(client, db, tenant, agent, fanout):
    """The resolve hook's FOR UPDATE select must execute on a worker thread."""
    import asyncio

    seen: list[bool] = []
    real_execute = db.execute

    def spy_execute(stmt, *args, **kwargs):
        if getattr(stmt, "_for_update_arg", None) is None:
            return real_execute(stmt, *args, **kwargs)
        try:
            asyncio.get_running_loop()
            seen.append(True)
        except RuntimeError:
            seen.append(False)
        return real_execute(stmt, *args, **kwargs)

    t = make_ticket(db, tenant, assigned_to=agent.id)
    db.execute = spy_execute
    try:
        r = _resolve(client, t, agent)
    finally:
        db.execute = real_execute
    assert r.status_code == 200, r.text
    assert seen and not any(seen), "DB execute ran on the event loop"
    assert _types(fanout) == ["rating_requested", "rating_request_created", "ticket_resolved"]
