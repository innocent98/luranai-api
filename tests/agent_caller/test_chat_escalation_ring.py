"""The escalation path must enqueue the ring without ever breaking the chat reply."""

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.api.v1.endpoints import chat


@pytest.mark.asyncio
async def test_ring_enqueues_both_jobs():
    ticket_id = uuid.uuid4()
    enqueue = AsyncMock()
    with (
        patch.object(chat.settings, "AGENT_ALERT_ENABLED", True),
        patch.object(chat.settings, "AGENT_ALERT_WAIT_SECONDS", 90),
        patch.object(chat, "enqueue_agent_alerts_async", enqueue),
    ):
        await chat._ring_agents(ticket_id)
    enqueue.assert_awaited_once_with(ticket_id, 90)


@pytest.mark.asyncio
async def test_ring_is_skipped_when_disabled():
    enqueue = AsyncMock()
    with (
        patch.object(chat.settings, "AGENT_ALERT_ENABLED", False),
        patch.object(chat, "enqueue_agent_alerts_async", enqueue),
    ):
        await chat._ring_agents(uuid.uuid4())
    enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_ring_is_skipped_for_a_missing_ticket_id():
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
        await chat._ring_agents(uuid.uuid4())  # must not raise


def test_every_ticket_creation_site_rings():
    """Guard against a future escalation branch forgetting the ring, or
    bypassing the re-escalation reuse guard (F1).

    Asserting on source text (not behavior) is deliberate: a missed call site
    fails silently in production — the chat reply still succeeds, nobody ever
    gets rung, and no behavioural test would ever catch the gap.

    Every escalation site must resolve its ticket through
    `_get_or_create_escalation_ticket` (never call `ticket_service.create_ticket`
    directly — a direct call reopens the F1 double-ticket bug) and every such
    resolution must sit next to a `_ring_agents` call, gated on `ticket_created`
    so a *reused* ticket doesn't ring a second time.
    """
    import inspect

    source = inspect.getsource(chat)
    direct_creates = source.count("ticket_service.create_ticket(")
    resolutions = source.count("= _get_or_create_escalation_ticket(")
    rings = source.count("await _ring_agents(")
    assert direct_creates == 1, (
        f"expected exactly one direct create_ticket call (inside the reuse-guard "
        f"helper itself), found {direct_creates} — new escalation sites must go "
        f"through _get_or_create_escalation_ticket instead"
    )
    assert (
        rings == resolutions
    ), f"{resolutions} escalation-ticket resolution sites but {rings} _ring_agents calls"
