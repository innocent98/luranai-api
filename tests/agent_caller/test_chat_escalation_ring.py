"""The escalation path must enqueue the ring without ever breaking the chat reply."""

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.api.v1.endpoints import chat


def _fake_ticket(ticket_id: uuid.UUID | None = None, updated_at: datetime | None = None):
    """Minimal duck-typed stand-in for a `TicketResponse` — `_ring_agents` only
    reads `.id` and `.updated_at` (the latter feeds the ARQ job-id `attempt`
    token, see N1)."""
    return SimpleNamespace(
        id=ticket_id or uuid.uuid4(),
        updated_at=updated_at or datetime(2026, 8, 17, 9, 0, 0, tzinfo=timezone.utc),
    )


@pytest.mark.asyncio
async def test_ring_enqueues_both_jobs():
    ticket = _fake_ticket()
    enqueue = AsyncMock()
    with (
        patch.object(chat.settings, "AGENT_ALERT_ENABLED", True),
        patch.object(chat.settings, "AGENT_ALERT_WAIT_SECONDS", 90),
        patch.object(chat, "enqueue_agent_alerts_async", enqueue),
    ):
        await chat._ring_agents(ticket)
    enqueue.assert_awaited_once_with(
        ticket.id, 90, attempt=int(ticket.updated_at.timestamp() * 1000)
    )


@pytest.mark.asyncio
async def test_ring_is_skipped_when_disabled():
    enqueue = AsyncMock()
    with (
        patch.object(chat.settings, "AGENT_ALERT_ENABLED", False),
        patch.object(chat, "enqueue_agent_alerts_async", enqueue),
    ):
        await chat._ring_agents(_fake_ticket())
    enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_ring_is_skipped_for_a_missing_ticket():
    enqueue = AsyncMock()
    with (
        patch.object(chat.settings, "AGENT_ALERT_ENABLED", True),
        patch.object(chat, "enqueue_agent_alerts_async", enqueue),
    ):
        await chat._ring_agents(None)
    enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_redis_failure_never_propagates():
    with (
        patch.object(chat.settings, "AGENT_ALERT_ENABLED", True),
        patch.object(
            chat, "enqueue_agent_alerts_async", AsyncMock(side_effect=RuntimeError("redis down"))
        ),
    ):
        await chat._ring_agents(_fake_ticket())  # must not raise


@pytest.mark.asyncio
async def test_ring_attempt_token_differs_for_a_distinct_ticket_state():
    """N1 regression: the ARQ job-id `attempt` must change between two ring
    calls for tickets whose `updated_at` differs — otherwise a re-ring (e.g.
    reopening a MISSED ticket) reuses the exact job id ARQ already holds for
    the earlier episode and gets silently swallowed, confirmed empirically
    against real Redis (see the N1 report entry)."""
    first_ticket = _fake_ticket(
        ticket_id=uuid.uuid4(), updated_at=datetime(2026, 8, 17, 2, 0, 0, tzinfo=timezone.utc)
    )
    reopened_ticket = _fake_ticket(
        ticket_id=first_ticket.id, updated_at=datetime(2026, 8, 17, 9, 0, 0, tzinfo=timezone.utc)
    )
    enqueue = AsyncMock()
    with (
        patch.object(chat.settings, "AGENT_ALERT_ENABLED", True),
        patch.object(chat, "enqueue_agent_alerts_async", enqueue),
    ):
        await chat._ring_agents(first_ticket)
        await chat._ring_agents(reopened_ticket)

    first_attempt = enqueue.await_args_list[0].kwargs["attempt"]
    second_attempt = enqueue.await_args_list[1].kwargs["attempt"]
    assert first_attempt != second_attempt


def test_every_ticket_creation_site_rings():
    """Guard against a future escalation branch forgetting the ring, or
    bypassing the re-escalation reuse guard (F1).

    Asserting on source text (not behavior) is deliberate: a missed call site
    fails silently in production — the chat reply still succeeds, nobody ever
    gets rung, and no behavioural test would ever catch the gap.

    Every escalation site must resolve its ticket through
    `_get_or_create_escalation_ticket` (never call `ticket_service.create_ticket`
    directly — a direct call reopens the F1 double-ticket bug; the helper now
    delegates to `TicketService.get_or_create_live_ticket`), every such
    resolution must sit next to a `_ring_agents` call, AND that call must
    actually be gated on `if ticket_created:` — the re-review's N2 finding is
    that the pre-fix version of this test only pinned the call *parity*
    (equal counts), which would also pass if the gating `if` were deleted and
    every site rang unconditionally.
    """
    import inspect

    source = inspect.getsource(chat)
    direct_creates = source.count("ticket_service.create_ticket(")
    resolutions = source.count("= _get_or_create_escalation_ticket(")
    rings = source.count("await _ring_agents(")
    gates = source.count("if ticket_created:")
    assert direct_creates == 0, (
        f"expected no direct create_ticket call in chat (the reuse guard lives in "
        f"TicketService.get_or_create_live_ticket), found {direct_creates} — new "
        f"escalation sites must go through _get_or_create_escalation_ticket instead"
    )
    assert (
        rings == resolutions
    ), f"{resolutions} escalation-ticket resolution sites but {rings} _ring_agents calls"
    assert gates == resolutions, (
        f"{resolutions} escalation-ticket resolution sites but only {gates} are "
        f"gated with 'if ticket_created:' — the ring must be conditional, not "
        f"just present"
    )
