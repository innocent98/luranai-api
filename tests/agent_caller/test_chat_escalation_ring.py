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
    """Guard against a future escalation branch forgetting the ring.

    Asserting on source text (not behavior) is deliberate: a missed call site
    fails silently in production — the chat reply still succeeds, nobody ever
    gets rung, and no behavioural test would ever catch the gap.
    """
    import inspect

    source = inspect.getsource(chat)
    creations = source.count("ticket_service.create_ticket(")
    rings = source.count("await _ring_agents(")
    assert rings == creations, f"{creations} create_ticket sites but {rings} _ring_agents calls"
