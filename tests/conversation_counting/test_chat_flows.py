"""Returning customers continue a conversation inside the window; beyond it a new one is counted.

Drives the real `send_message` pipeline (LLM stubbed) so the reuse rules, the reopen rules and the
402 gate are exercised exactly as the widget, the support page and the customer dashboard hit them.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import select

from app.db.models.conversation import ConversationSession, SessionStatus
from app.db.models.customer_user import CustomerUser
from app.schemas.conversation import MessageRequest
from app.services import conversation_counting as cc
from app.services.conversation_counting import count_conversations_this_month
from tests.conversation_counting.conftest import seed_session
from tests.whatsapp.conftest import make_tenant


def _req(tenant, visitor=None, *, text="hello there", session_id=None, identifier="anon-1"):
    metadata = {"visitor_token": visitor.public_token, "entry_point": "widget"} if visitor else None
    return MessageRequest(
        message=text,
        session_id=session_id,
        tenant_id=tenant.id,
        user_identifier=identifier,
        metadata=metadata,
    )


async def _send(db, request, **kwargs):
    from app.api.v1.endpoints.chat import send_message

    return await send_message(request, db, **kwargs)


def _rows(db, tenant):
    db.expire_all()
    return (
        db.execute(select(ConversationSession).where(ConversationSession.tenant_id == tenant.id))
        .scalars()
        .all()
    )


def _age(db, session_id, hours, status):
    row = db.get(ConversationSession, session_id)
    from datetime import timedelta

    from app.utils.datetime import utcnow_naive

    row.status = status
    row.last_message_at = utcnow_naive() - timedelta(hours=hours)
    db.commit()


# ------------------------------------------------------------------ anonymous visitor
async def test_anonymous_return_inside_window_continues_and_is_not_recounted(
    db, tenant, make_visitor, llm
):
    visitor = make_visitor()
    first = await _send(db, _req(tenant, visitor))
    _age(db, first.session_id, 5.98, SessionStatus.ABANDONED)

    again = await _send(db, _req(tenant, visitor, text="I am back"))

    assert again.session_id == first.session_id
    rows = _rows(db, tenant)
    assert len(rows) == 1 and rows[0].status == SessionStatus.ACTIVE
    assert count_conversations_this_month(db, tenant.id) == 1


async def test_anonymous_resolved_inside_window_is_reopened_on_the_same_row(
    db, tenant, make_visitor, llm
):
    visitor = make_visitor()
    first = await _send(db, _req(tenant, visitor))
    _age(db, first.session_id, 2, SessionStatus.RESOLVED)

    again = await _send(db, _req(tenant, visitor, text="one more question"))

    assert again.session_id == first.session_id
    assert [r.status for r in _rows(db, tenant)] == [SessionStatus.ACTIVE]


async def test_anonymous_return_beyond_window_is_a_new_counted_conversation(
    db, tenant, make_visitor, llm
):
    visitor = make_visitor()
    first = await _send(db, _req(tenant, visitor))
    _age(db, first.session_id, 6.05, SessionStatus.ABANDONED)

    again = await _send(db, _req(tenant, visitor, text="hello again"))

    assert again.session_id != first.session_id
    assert count_conversations_this_month(db, tenant.id) == 2
    old = db.get(ConversationSession, first.session_id)
    assert old.status == SessionStatus.ABANDONED  # history untouched


async def test_resolved_beyond_window_is_new_too(db, tenant, make_visitor, llm):
    visitor = make_visitor()
    first = await _send(db, _req(tenant, visitor))
    _age(db, first.session_id, 6.05, SessionStatus.RESOLVED)
    again = await _send(db, _req(tenant, visitor))
    assert again.session_id != first.session_id


async def test_escalated_thread_is_continued_whatever_its_age(db, tenant, make_visitor, llm):
    visitor = make_visitor()
    seeded = seed_session(
        db, tenant.id, status=SessionStatus.ESCALATED, hours_ago=50, visitor_id=visitor.id
    )
    again = await _send(db, _req(tenant, visitor))
    assert again.session_id == seeded.id
    assert count_conversations_this_month(db, tenant.id) == 1


# ------------------------------------------------------------------ explicit session id
async def test_explicit_session_beyond_window_opens_a_new_one_and_later_sends_follow_it(
    db, tenant, make_visitor, llm
):
    visitor = make_visitor()
    first = await _send(db, _req(tenant, visitor))
    _age(db, first.session_id, 7, SessionStatus.ABANDONED)

    second = await _send(db, _req(tenant, visitor, session_id=first.session_id, text="back"))
    assert second.session_id != first.session_id

    # A client that never adopts the new id keeps sending the old one: it must land in the new
    # conversation, not open a third.
    third = await _send(db, _req(tenant, visitor, session_id=first.session_id, text="and more"))
    assert third.session_id == second.session_id
    assert count_conversations_this_month(db, tenant.id) == 2


async def test_explicit_stale_session_without_visitor_still_converges(db, tenant, llm):
    first = await _send(db, _req(tenant, identifier="anon-x"))
    _age(db, first.session_id, 7, SessionStatus.RESOLVED)

    second = await _send(db, _req(tenant, session_id=first.session_id, identifier="anon-x"))
    third = await _send(db, _req(tenant, session_id=first.session_id, identifier="anon-x"))

    assert second.session_id != first.session_id
    assert third.session_id == second.session_id
    assert count_conversations_this_month(db, tenant.id) == 2


async def test_voice_style_request_without_identifier_inherits_it(db, tenant, llm):
    """Voice turns send only session_id + tenant_id; a stale session must still convert."""
    first = await _send(db, _req(tenant, identifier="voice-user"))
    _age(db, first.session_id, 9, SessionStatus.ABANDONED)
    request = MessageRequest(message="hi", session_id=first.session_id, tenant_id=tenant.id)
    second = await _send(db, request)
    assert second.session_id != first.session_id
    assert db.get(ConversationSession, second.session_id).user_identifier == "voice-user"


async def test_explicit_session_inside_window_reactivates_the_same_row(
    db, tenant, make_visitor, llm
):
    visitor = make_visitor()
    first = await _send(db, _req(tenant, visitor))
    _age(db, first.session_id, 5.9, SessionStatus.RESOLVED)
    again = await _send(db, _req(tenant, visitor, session_id=first.session_id, text="thanks!"))
    assert again.session_id == first.session_id
    assert count_conversations_this_month(db, tenant.id) == 1


async def test_widget_greeting_does_not_reopen_or_recount_a_resolved_session(
    db, tenant, make_visitor, llm
):
    visitor = make_visitor()
    first = await _send(db, _req(tenant, visitor))
    _age(db, first.session_id, 30, SessionStatus.RESOLVED)
    again = await _send(db, _req(tenant, visitor, session_id=first.session_id, text="__init__"))
    assert again.session_id == first.session_id
    assert db.get(ConversationSession, first.session_id).status == SessionStatus.RESOLVED
    assert count_conversations_this_month(db, tenant.id) == 1


# ------------------------------------------------------------------ authed customer
def _customer(db) -> CustomerUser:
    customer = CustomerUser(
        id=uuid4(),
        full_name="Window Customer",
        email=f"w-{uuid4().hex[:6]}@example.com",
        hashed_password="x",
        is_active=True,
    )
    db.add(customer)
    db.commit()
    return customer


async def test_authed_customer_window(db, tenant, llm):
    customer = _customer(db)
    cid = customer.id
    first = await _send(db, _req(tenant, identifier="cust"), authed_customer_id=cid)

    _age(db, first.session_id, 5.9, SessionStatus.RESOLVED)
    inside = await _send(db, _req(tenant, identifier="cust"), authed_customer_id=cid)
    assert inside.session_id == first.session_id
    assert count_conversations_this_month(db, tenant.id) == 1

    _age(db, first.session_id, 6.1, SessionStatus.RESOLVED)
    beyond = await _send(db, _req(tenant, identifier="cust"), authed_customer_id=cid)
    assert beyond.session_id != first.session_id
    assert count_conversations_this_month(db, tenant.id) == 2


# ------------------------------------------------------------------ limit: 402 and ungated paths
def _at_cap(db):
    tenant = make_tenant(db, max_conversations=1)
    seed_session(db, tenant.id, status=SessionStatus.RESOLVED, hours_ago=100, created_hours_ago=1)
    return tenant


async def test_new_conversation_at_the_cap_gets_the_402(db, llm):
    from fastapi import HTTPException

    tenant = _at_cap(db)
    with pytest.raises(HTTPException) as exc:
        await _send(db, _req(tenant, identifier="newcomer"))
    assert exc.value.status_code == 402
    detail = exc.value.detail
    assert detail["error"] == "conversation_limit_reached"
    assert detail["message"] == "Monthly conversation limit of 1 reached."
    assert detail["current_count"] == 1 and detail["limit"] == 1
    assert len(_rows(db, tenant)) == 1  # nothing was created


async def test_continuing_inside_the_window_is_not_blocked_at_the_cap(db, make_visitor, llm):
    tenant = make_tenant(db, max_conversations=1)
    visitor = make_visitor(tenant.id)
    seeded = seed_session(
        db, tenant.id, status=SessionStatus.ABANDONED, hours_ago=2, visitor_id=visitor.id
    )
    again = await _send(db, _req(tenant, visitor))
    assert again.session_id == seeded.id


async def test_reactivating_beyond_the_window_at_the_cap_is_a_new_conversation_so_402(db, llm):
    from fastapi import HTTPException

    tenant = _at_cap(db)
    stale = db.execute(
        select(ConversationSession).where(ConversationSession.tenant_id == tenant.id)
    ).scalar_one()
    request = _req(tenant, session_id=stale.id, identifier=stale.user_identifier)
    with pytest.raises(HTTPException) as exc:
        await _send(db, request)
    assert exc.value.status_code == 402
    assert db.get(ConversationSession, stale.id).status == SessionStatus.RESOLVED  # untouched


# ------------------------------------------------------------------ POST /chat/sessions
def test_create_session_endpoint_is_gated_with_the_same_402(client, db):
    tenant = _at_cap(db)
    resp = client.post(
        "/api/v1/chat/sessions", json={"tenant_id": str(tenant.id), "user_identifier": "u1"}
    )
    assert resp.status_code == 402
    assert resp.json()["detail"] == {
        "error": "conversation_limit_reached",
        "message": "Monthly conversation limit of 1 reached.",
        "current_count": 1,
        "limit": 1,
        "resets_at": cc.conversation_usage(db, tenant.id).resets_at.isoformat(),
    }


def test_create_session_endpoint_still_works_under_the_cap(client, db):
    tenant = make_tenant(db, max_conversations=2)
    resp = client.post(
        "/api/v1/chat/sessions", json={"tenant_id": str(tenant.id), "user_identifier": "u1"}
    )
    assert resp.status_code == 201, resp.text
    assert count_conversations_this_month(db, tenant.id) == 1
