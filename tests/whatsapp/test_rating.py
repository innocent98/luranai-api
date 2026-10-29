"""Rating over WhatsApp: the request is forwarded, remembered on the chat, and a bare 1-5 answers it.

The write goes through `ReviewService` (the widget's service), so attribution is compared
against a rating made the widget way.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.core.websocket import ConnectionManager
from app.db.models.conversation import (
    ConversationSession,
    Message,
    Ticket,
    TicketPriority,
    TicketStatus,
)
from app.db.models.review import RatingRequest, RatingSource, SessionRating
from app.db.models.tenant import Tenant
from app.db.models.user import User, UserRole
from app.db.models.whatsapp import WhatsAppChat, WhatsAppConnection
from app.schemas.review import SessionRatingCreate
from app.services import whatsapp_dispatch
from app.services.rating_request_service import (
    RatingRequestService,
    customer_frame,
    fanout_rating_request,
)
from app.services.review_service import ReviewService
from app.services.whatsapp_inbound import handle_event
from app.services.whatsapp_outbound import send_text
from app.utils.datetime import utcnow_aware
from tests.whatsapp.conftest import connect, inbound, make_tenant
from tests.whatsapp.test_outbound import _NoCloseSession

pytestmark = pytest.mark.asyncio

PROMPT = "How was your chat with Sam? Reply with a number from 1 (poor) to 5 (great)."


@pytest.fixture
def conn(db, fake_sidecar, queue):
    return connect(db, fake_sidecar, make_tenant(db))


@pytest.fixture
def manager(monkeypatch):
    mgr = ConnectionManager()
    monkeypatch.setattr(mgr, "_publish_event", AsyncMock())
    return mgr


@pytest.fixture(autouse=True)
def _own_db(db, monkeypatch):
    monkeypatch.setattr("app.db.session.SessionLocal", lambda: _NoCloseSession(db))


def _chat(db, conn, ref: str = "1234567890@lid") -> WhatsAppChat:
    from app.channels import identity

    db.expire_all()
    return db.execute(
        select(WhatsAppChat).where(
            WhatsAppChat.connection_id == conn.id,
            WhatsAppChat.contact_hmac == identity.contact_hmac(ref),
        )
    ).scalar_one()


def _ratings(db):
    db.expire_all()
    return list(db.execute(select(SessionRating)).scalars())


class Scenario:
    """A WhatsApp customer in a conversation an agent ("Sam Agent") has been working."""

    def __init__(self, db, conn, ref: str = "1234567890@lid") -> None:
        self.db, self.conn, self.ref = db, conn, ref

    async def start(self):
        await handle_event(self.db, inbound(self.conn, ref=self.ref))
        self.chat = _chat(self.db, self.conn, self.ref)
        self.session = self.db.get(ConversationSession, self.chat.session_id)
        self.agent = User(
            email=f"{uuid.uuid4().hex[:8]}@example.com",
            hashed_password="x",
            full_name="Sam Agent",
            role=UserRole.SUPPORT_AGENT,
            tenant_id=self.conn.tenant_id,
            is_active=True,
        )
        self.db.add(self.agent)
        self.db.flush()
        self.ticket = Ticket(
            tenant_id=self.conn.tenant_id,
            session_id=self.session.id,
            ticket_number=f"TKT-WA-{uuid.uuid4().hex[:8]}",
            summary="Help",
            priority=TicketPriority.MEDIUM,
            status=TicketStatus.IN_PROGRESS,
            assigned_to=self.agent.id,
        )
        self.db.add(self.ticket)
        self.db.commit()
        return self

    def request(self):
        result = RatingRequestService(self.db).create_request(
            self.ticket.id, source=RatingSource.AUTO_RESOLVE, requested_by=None
        )
        self.result = result
        return result

    async def deliver_prompt(self, manager, queue) -> dict:
        """Mint a request, push the customer frame through the real fanout hook, then run the
        queued send job body against the fake sidecar (which is when the chat remembers it)."""
        result = self.request()
        queue.sends.clear()
        await manager.fanout_event(
            self.session.id, customer_frame(result), to_customer=True, to_agents=False
        )
        assert len(queue.sends) == 1, queue.sends
        job = queue.sends[0]
        outcome = await send_text(
            self.db,
            job["connection_id"],
            job["chat_id"],
            job["text"],
            1,
            job["guard"],
            job["rating_request_id"],
        )
        assert outcome == "sent"
        queue.sends.clear()
        self.db.expire_all()
        return job


# ------------------------------------------------------------------ forwarding the request
async def test_rating_request_frame_maps_to_the_prompt_plus_instruction_and_never_the_token():
    frame = {
        "type": "rating_requested",
        "request_id": "r-1",
        "token": "SECRET-TOKEN-VALUE",
        "agent_name": "Sam",
        "content": "How was your chat with Sam?",
    }
    key, text = whatsapp_dispatch.frame_text(frame)
    assert key == "rating_requested:r-1" and text == PROMPT
    assert "SECRET" not in text and "SECRET" not in key
    no_agent = {**frame, "agent_name": None, "content": "How was your chat with our team?"}
    assert whatsapp_dispatch.frame_text(no_agent)[1] == (
        "How was your chat with our team? Reply with a number from 1 (poor) to 5 (great)."
    )


async def test_the_real_rating_frame_reaches_whatsapp_and_the_chat_remembers_it(
    db, conn, queue, llm, manager
):
    sc = await Scenario(db, conn).start()
    job = await sc.deliver_prompt(manager, queue)
    assert job["text"] == PROMPT and job["guard"] is False
    assert job["key"] == f"rating_requested:{sc.result.request.id}"
    assert job["rating_request_id"] == sc.result.request.id
    assert "token" not in job["text"].lower() and sc.result.token not in job["text"]
    chat = _chat(db, conn)
    assert chat.rating_request_id == sc.result.request.id
    assert chat.rating_request_expires_at is not None
    # the stored message is the plain prompt (the widget card); the WhatsApp text adds the how-to
    msg = db.get(Message, sc.result.message.id)
    assert msg.content == "How was your chat with Sam?"


async def test_the_chat_does_not_remember_a_prompt_that_was_never_sent(
    db, conn, fake_sidecar, queue, llm, manager
):
    sc = await Scenario(db, conn).start()
    result = sc.request()
    fake_sidecar.send_error = (500, "boom")
    from app.services.whatsapp_outbound import RetrySend

    with pytest.raises(RetrySend):
        await send_text(db, conn.id, sc.chat.id, PROMPT, 1, False, result.request.id)
    assert _chat(db, conn).rating_request_id is None
    # ... so a stray "5" is just text
    assert await handle_event(db, inbound(conn, text="5")) == "ai_reply"
    assert _ratings(db) == []


@pytest.mark.parametrize("who", ["other_session", "other_tenant", "fulfilled"])
async def test_a_prompt_for_a_request_that_is_not_this_chats_is_not_remembered(
    db, fake_sidecar, queue, llm, manager, conn, who
):
    sc = await Scenario(db, conn).start()
    if who == "other_session":
        other = await Scenario(db, conn, ref="999@lid").start()
        request_id = other.request().request.id
    elif who == "other_tenant":
        conn2 = connect(db, fake_sidecar, make_tenant(db), "2348000000041")
        other = await Scenario(db, conn2).start()
        request_id = other.request().request.id
    else:
        result = sc.request()
        ReviewService(db).rate_request_by_id(
            sc.session, result.request.id, score=3, user_identifier=sc.session.user_identifier
        )
        request_id = result.request.id
    await send_text(db, conn.id, sc.chat.id, PROMPT, 1, False, request_id)
    assert _chat(db, conn).rating_request_id is None


# ------------------------------------------------------------------ the reply
async def test_a_numeric_reply_records_the_rating_exactly_like_a_widget_rating(
    db, conn, queue, llm, manager
):
    sc = await Scenario(db, conn).start()
    await sc.deliver_prompt(manager, queue)
    llm.calls = 0
    assert await handle_event(db, inbound(conn, text="5")) == "rating_recorded"
    assert llm.calls == 0  # never reaches the AI
    (rating,) = _ratings(db)
    assert rating.score == 5 and rating.session_id == sc.session.id
    assert rating.tenant_id == conn.tenant_id and rating.rated_agent_id == sc.agent.id
    assert rating.source == RatingSource.AUTO_RESOLVE
    assert rating.rating_request_id == sc.result.request.id
    assert rating.user_identifier == sc.session.user_identifier
    assert db.get(RatingRequest, rating.rating_request_id).fulfilled_at is not None
    # one acknowledgement, sent unguarded (a courtesy line, not an AI answer)
    assert [(s["text"], s["guard"]) for s in queue.sends] == [
        ("Thanks! We've recorded your 5/5.", False)
    ]
    assert queue.sends[0]["key"] == f"rating_ack:{sc.result.request.id}"
    chat = _chat(db, conn)
    assert chat.rating_request_id is None and chat.rating_request_expires_at is None
    # staff see it attributed to the agent and the ticket
    listing = ReviewService(db).list_session_ratings(tenant_id=conn.tenant_id)
    (row,) = listing.ratings
    assert row.rated_agent_name == "Sam Agent" and row.ticket_id == sc.ticket.id

    # ... the same attribution a widget (token) rating of an identical request gets
    other_session = ConversationSession(
        tenant_id=conn.tenant_id, user_identifier="widget-visitor", status=sc.session.status
    )
    db.add(other_session)
    db.flush()
    twin = Ticket(
        tenant_id=conn.tenant_id,
        session_id=other_session.id,
        ticket_number=f"TKT-W-{uuid.uuid4().hex[:8]}",
        summary="Help",
        priority=TicketPriority.MEDIUM,
        status=TicketStatus.IN_PROGRESS,
        assigned_to=sc.agent.id,
    )
    db.add(twin)
    db.commit()
    made = RatingRequestService(db).create_request(
        twin.id, source=RatingSource.AUTO_RESOLVE, requested_by=None
    )
    widget = ReviewService(db).create_session_rating(
        SessionRatingCreate(
            score=5,
            session_id=other_session.id,
            user_identifier="widget-visitor",
            rating_request_token=made.token,
        )
    )
    assert (widget.rated_agent_id, widget.source, widget.rated_party) == (
        rating.rated_agent_id,
        rating.source,
        "human",
    )


@pytest.mark.parametrize(
    "reply,score",
    [
        ("5", 5),
        (" 4 ", 4),
        ("3/5", 3),
        ("3 / 5", 3),
        ("2 stars", 2),
        ("1 star", 1),
        ("5 STARS", 5),
        ("5⭐⭐", 5),
        ("4⭐", 4),
        ("4.", 4),
        ("5!!", 5),
        ("2 .", 2),
        ("\n3\n", 3),
    ],
)
async def test_accepted_reply_formats(db, conn, queue, llm, manager, reply, score):
    sc = await Scenario(db, conn).start()
    await sc.deliver_prompt(manager, queue)
    assert await handle_event(db, inbound(conn, text=reply)) == "rating_recorded"
    assert [r.score for r in _ratings(db)] == [score]
    assert queue.sends[0]["text"] == f"Thanks! We've recorded your {score}/5."


@pytest.mark.parametrize(
    "reply",
    [
        "6",
        "0",
        "10",
        "55",
        "5 please",
        "four",
        "3 out of 5",
        "4/10",
        "5 and 5",
        "-3",
        "1.5",
        "👍",
        "5 ⭐ thanks",
    ],
)
async def test_anything_else_goes_through_the_normal_flow_and_the_request_stays_open(
    db, conn, queue, llm, manager, reply
):
    sc = await Scenario(db, conn).start()
    await sc.deliver_prompt(manager, queue)
    assert await handle_event(db, inbound(conn, text=reply)) == "ai_reply"
    assert _ratings(db) == [] and _chat(db, conn).rating_request_id == sc.result.request.id


async def test_a_digit_with_no_open_request_is_ordinary_text(db, conn, queue, llm):
    await handle_event(db, inbound(conn))
    llm.calls = 0
    assert await handle_event(db, inbound(conn, text="5")) == "ai_reply"
    assert llm.calls == 1 and _ratings(db) == []


async def test_a_second_answer_is_ordinary_text_not_a_second_rating(db, conn, queue, llm, manager):
    sc = await Scenario(db, conn).start()
    await sc.deliver_prompt(manager, queue)
    assert await handle_event(db, inbound(conn, text="5")) == "rating_recorded"
    queue.sends.clear()
    assert await handle_event(db, inbound(conn, text="1")) == "ai_reply"
    assert [r.score for r in _ratings(db)] == [5]
    assert [s["key"].startswith("ai:") for s in queue.sends] == [True]


async def test_a_redelivered_reply_is_deduplicated(db, conn, queue, llm, manager):
    sc = await Scenario(db, conn).start()
    await sc.deliver_prompt(manager, queue)
    ev = inbound(conn, text="4", mid="RATE-1")
    assert await handle_event(db, ev) == "rating_recorded"
    assert await handle_event(db, dict(ev, id="redelivery")) == "duplicate"
    assert len(_ratings(db)) == 1 and len(queue.sends) == 1


async def test_an_expired_chat_window_makes_the_reply_ordinary_text(db, conn, queue, llm, manager):
    sc = await Scenario(db, conn).start()
    await sc.deliver_prompt(manager, queue)
    chat = _chat(db, conn)
    chat.rating_request_expires_at = utcnow_aware() - timedelta(minutes=1)
    db.commit()
    assert await handle_event(db, inbound(conn, text="5")) == "ai_reply"
    assert _ratings(db) == [] and _chat(db, conn).rating_request_id is None


async def test_a_request_expired_in_the_service_is_refused_even_if_the_chat_still_remembers_it(
    db, conn, queue, llm, manager
):
    sc = await Scenario(db, conn).start()
    await sc.deliver_prompt(manager, queue)
    db.get(RatingRequest, sc.result.request.id).expires_at = utcnow_aware() - timedelta(seconds=1)
    db.commit()
    assert await handle_event(db, inbound(conn, text="5")) == "ai_reply"
    assert _ratings(db) == [] and _chat(db, conn).rating_request_id is None


async def test_the_chat_window_matches_the_seven_day_request_expiry(db, conn, queue, llm, manager):
    sc = await Scenario(db, conn).start()
    await sc.deliver_prompt(manager, queue)
    left = _chat(db, conn).rating_request_expires_at
    from app.utils.datetime import ensure_utc_aware

    assert (
        timedelta(days=6, hours=23) < ensure_utc_aware(left) - utcnow_aware() <= timedelta(days=7)
    )


async def test_already_rated_on_the_widget_means_the_whatsapp_reply_is_plain_text(
    db, conn, queue, llm, manager
):
    sc = await Scenario(db, conn).start()
    await sc.deliver_prompt(manager, queue)
    ReviewService(db).create_session_rating(
        SessionRatingCreate(
            score=2,
            session_id=sc.session.id,
            user_identifier=sc.session.user_identifier,
            rating_request_token=sc.result.token,
        )
    )
    assert await handle_event(db, inbound(conn, text="5")) == "ai_reply"
    assert [r.score for r in _ratings(db)] == [2]


# ------------------------------------------------------------------ cannot be aimed elsewhere
async def test_another_customer_in_the_same_number_cannot_rate_someone_elses_conversation(
    db, conn, queue, llm, manager
):
    a = await Scenario(db, conn, ref="111@lid").start()
    await a.deliver_prompt(manager, queue)
    await handle_event(db, inbound(conn, ref="222@lid"))  # a different customer
    assert await handle_event(db, inbound(conn, ref="222@lid", text="1")) == "ai_reply"
    assert (
        _ratings(db) == [] and _chat(db, conn, "111@lid").rating_request_id == a.result.request.id
    )


async def test_a_chat_row_pointing_at_a_foreign_request_records_nothing(
    db, fake_sidecar, queue, llm, manager, conn
):
    """Even if a chat row were corrupted to hold another customer's / tenant's request id, the
    reply is bound to the customer (user identifier) and tenant and records nothing."""
    victim = await Scenario(db, conn, ref="111@lid").start()
    victim.request()
    attacker_chat_session = await Scenario(db, conn, ref="222@lid").start()
    chat = _chat(db, conn, "222@lid")
    chat.rating_request_id = victim.result.request.id  # forged state
    chat.rating_request_expires_at = utcnow_aware() + timedelta(days=1)
    db.commit()
    assert await handle_event(db, inbound(conn, ref="222@lid", text="1")) == "ai_reply"
    assert _ratings(db) == [] and attacker_chat_session.session.id != victim.session.id

    conn2 = connect(db, fake_sidecar, make_tenant(db), "2348000000042")
    await handle_event(db, inbound(conn2, ref="333@lid"))
    chat2 = _chat(db, conn2, "333@lid")
    chat2.rating_request_id = victim.result.request.id
    chat2.rating_request_expires_at = utcnow_aware() + timedelta(days=1)
    db.commit()
    assert await handle_event(db, inbound(conn2, ref="333@lid", text="1")) == "ai_reply"
    assert _ratings(db) == []
    assert db.get(RatingRequest, victim.result.request.id).fulfilled_at is None


async def test_the_reply_never_names_a_session_or_request_so_event_fields_cannot_aim_it(
    db, conn, queue, llm, manager
):
    """Extra fields on the event (a session id, a request id, a tenant) are ignored."""
    a = await Scenario(db, conn, ref="111@lid").start()
    await a.deliver_prompt(manager, queue)
    b = await Scenario(db, conn, ref="222@lid").start()
    b.request()
    ev = inbound(
        conn,
        ref="111@lid",
        text="5",
        session_id=str(b.session.id),
        rating_request_id=str(b.result.request.id),
        tenant_id=str(uuid.uuid4()),
    )
    assert await handle_event(db, ev) == "rating_recorded"
    (rating,) = _ratings(db)
    assert rating.session_id == a.session.id and rating.rated_agent_id == a.agent.id


# ------------------------------------------------------------------ interplay with the guards
async def test_a_reply_is_recorded_even_while_the_owner_has_paused_the_ai(
    db, conn, queue, llm, manager
):
    sc = await Scenario(db, conn).start()
    await sc.deliver_prompt(manager, queue)
    chat = _chat(db, conn)
    chat.human_until = utcnow_aware() + timedelta(minutes=30)
    db.get(WhatsAppConnection, conn.id).ai_enabled = False
    db.commit()
    assert await handle_event(db, inbound(conn, text="4")) == "rating_recorded"
    assert [r.score for r in _ratings(db)] == [4]
    assert queue.sends == []  # recorded, but no automatic thank-you into the owner's own chat


async def test_an_inactive_subscription_leaves_the_reply_to_the_plan_rules(
    db, conn, queue, llm, manager
):
    sc = await Scenario(db, conn).start()
    await sc.deliver_prompt(manager, queue)
    db.get(Tenant, conn.tenant_id).subscription_status = "canceled"
    db.commit()
    assert await handle_event(db, inbound(conn, text="5")) == "plan_limit"
    assert _ratings(db) == [] and queue.sends == []


async def test_the_acknowledgement_failing_to_queue_does_not_undo_or_duplicate_the_rating(
    db, conn, queue, llm, manager, monkeypatch
):
    sc = await Scenario(db, conn).start()
    await sc.deliver_prompt(manager, queue)

    async def boom(*a, **k):
        raise RuntimeError("redis down")

    from app.services import whatsapp_enqueue

    monkeypatch.setattr(whatsapp_enqueue, "enqueue_send", boom)
    assert await handle_event(db, inbound(conn, text="5")) == "rating_recorded"
    assert len(_ratings(db)) == 1


async def test_fanout_rating_request_is_what_feeds_the_hook(db, conn, queue, llm, manager):
    """The service's own fanout (customer frame + token-free agent frame) drives the forward."""
    sc = await Scenario(db, conn).start()
    result = sc.request()
    queue.sends.clear()  # the opening AI reply
    from app.core.websocket import connection_manager

    seen = []

    async def capture(session_id, message, to_customer, to_agents, **kw):
        seen.append((message["type"], to_customer))
        await manager.fanout_event(session_id, message, to_customer, to_agents)

    connection_manager.fanout_event, original = capture, connection_manager.fanout_event
    try:
        await fanout_rating_request(result)
    finally:
        connection_manager.fanout_event = original
    assert ("rating_requested", True) in seen and ("rating_request_created", False) in seen
    assert [s["key"] for s in queue.sends] == [f"rating_requested:{result.request.id}"]


async def test_a_request_from_an_earlier_conversation_does_not_capture_a_digit_in_a_new_one(
    db, conn, queue, llm, manager
):
    sc = await Scenario(db, conn).start()
    await sc.deliver_prompt(manager, queue)
    old = db.get(ConversationSession, sc.session.id)
    old.status = __import__("app.db.models.conversation", fromlist=["x"]).SessionStatus.RESOLVED
    db.commit()
    # the customer comes back later: a NEW conversation, and answers the AI's question with "2"
    assert await handle_event(db, inbound(conn, text="hi again")) == "ai_reply"
    assert _chat(db, conn).session_id != sc.session.id
    assert await handle_event(db, inbound(conn, text="2")) == "ai_reply"
    assert _ratings(db) == []


async def test_clearing_a_stale_request_never_wipes_a_newer_prompt(db, conn, queue, llm, manager):
    from app.services.whatsapp_inbound import _clear_rating

    sc = await Scenario(db, conn).start()
    await sc.deliver_prompt(manager, queue)
    chat = _chat(db, conn)
    newer = chat.rating_request_id
    _clear_rating(db, chat, uuid.uuid4())  # an older reply's cleanup, for a different request
    assert _chat(db, conn).rating_request_id == newer
    _clear_rating(db, chat, newer)
    assert _chat(db, conn).rating_request_id is None
