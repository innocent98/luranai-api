"""Escalation emails must never block (or fail) the turn that escalates."""

import asyncio
import time
from unittest.mock import AsyncMock, patch

from app.api.v1.endpoints import chat as chat_module
from app.core.config import settings

_KW = dict(
    recipients=[("a@example.test", "A"), ("b@example.test", "B"), ("c@example.test", "C")],
    tenant_name="T",
    ticket_number="TKT-1",
    ticket_id="00000000-0000-0000-0000-000000000001",
    escalation_reason="asked",
    customer_query="get me a human",
    priority_label="HIGH",
    contact_email=None,
)


async def test_schedule_returns_immediately_and_sends_to_every_recipient():
    async def slow_send(**_k):
        await asyncio.sleep(0.2)
        return True

    send = AsyncMock(side_effect=slow_send)
    with patch.object(chat_module, "send_ticket_escalation_email", send):
        t0 = time.monotonic()
        chat_module._schedule_escalation_emails(**_KW)
        assert time.monotonic() - t0 < 0.05  # 3 x 0.2s of mail did not run inline
        assert send.await_count == 0
        await asyncio.gather(*chat_module._background_notifications)
    assert [c.kwargs["email_to"] for c in send.await_args_list] == [
        "a@example.test",
        "b@example.test",
        "c@example.test",
    ]
    assert send.await_args_list[0].kwargs["ticket_number"] == "TKT-1"
    assert send.await_args_list[0].kwargs["ticket_priority"] == "HIGH"


async def test_one_failing_recipient_is_logged_and_does_not_stop_the_rest():
    send = AsyncMock(side_effect=[RuntimeError("smtp down"), False, True])
    with patch.object(chat_module, "send_ticket_escalation_email", send):
        chat_module._schedule_escalation_emails(**_KW)
        await asyncio.gather(*chat_module._background_notifications)
    assert send.await_count == 3


async def test_schedule_never_raises_into_the_turn():
    with patch.object(chat_module.asyncio, "create_task", side_effect=RuntimeError("no loop")):
        chat_module._schedule_escalation_emails(**_KW)  # must not raise


def test_escalating_turn_does_not_wait_for_slow_email(client, db, monkeypatch):
    """End to end through /chat/message: three recipients x 2 s of SMTP must not
    be on the escalating turn's critical path."""
    from uuid import uuid4

    from app.db.models.user import User, UserRole
    from tests.api.v1.test_chat_flow_router import _create_tenant_with_router_flow

    tenant = _create_tenant_with_router_flow(db)
    for i in range(3):
        db.add(
            User(
                email=f"esc_{uuid4().hex[:6]}_{i}@example.com",
                hashed_password="$2b$12$placeholder_hash_not_real",
                full_name=f"Agent {i}",
                role=UserRole.SUPPORT_AGENT,
                tenant_id=tenant.id,
                is_active=True,
            )
        )
    db.commit()

    # REAL send_ticket_escalation_email -> send_email, with the blocking SMTP
    # call (`emails.Message.send`) faked by a blocking sleep.
    monkeypatch.setattr(settings, "SMTP_HOST", "smtp.invalid")
    monkeypatch.setattr(settings, "EMAILS_FROM_EMAIL", "noreply@example.test")

    def blocking_send(self, **_k):
        time.sleep(2)
        return type("R", (), {"status_code": 250})()

    with (
        patch("emails.Message.send", blocking_send),
        patch.object(chat_module, "enqueue_agent_alerts_async", AsyncMock()),
    ):
        t0 = time.monotonic()
        resp = client.post(
            "/api/v1/chat/message",
            json={
                "tenant_id": str(tenant.id),
                "session_id": None,
                "message": "__talk_to_human__",
                "user_identifier": "slow_email_user",
                "metadata": {"source": "pytest"},
            },
        )
        elapsed = time.monotonic() - t0

    assert resp.status_code == 200
    assert resp.json()["should_escalate"] is True
    assert resp.json()["ticket_id"] is not None
    assert elapsed < 2.0, f"escalating turn took {elapsed:.1f}s; emails are still inline"


async def test_event_loop_stays_responsive_during_real_blocking_send(monkeypatch):
    """A blocking SMTP send must run off the loop: a heartbeat keeps ticking and
    scheduling returns immediately (this is what the voice SSE stream needs)."""
    monkeypatch.setattr(settings, "SMTP_HOST", "smtp.invalid")
    monkeypatch.setattr(settings, "EMAILS_FROM_EMAIL", "noreply@example.test")
    calls = []

    def blocking_send(self, **_k):
        calls.append(1)
        time.sleep(1.0)
        return type("R", (), {"status_code": 250})()

    ticks = 0
    stop = asyncio.Event()

    async def heartbeat():
        nonlocal ticks
        while not stop.is_set():
            await asyncio.sleep(0.05)
            ticks += 1

    with patch("emails.Message.send", blocking_send):
        hb = asyncio.create_task(heartbeat())
        t0 = time.monotonic()
        chat_module._schedule_escalation_emails(**{**_KW, "recipients": [("a@example.test", "A")]})
        assert time.monotonic() - t0 < 0.5
        await asyncio.gather(*chat_module._background_notifications)
        stop.set()
        await hb
    assert calls == [1]
    assert ticks >= 10, f"event loop was blocked: only {ticks} heartbeat ticks during the send"
