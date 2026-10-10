"""Short-lived dedupe of voice turns, keyed on the conversation state.

ElevenLabs abandons a custom-LLM request it considers slow and re-issues the
same turn (same `messages` history). Each of those requests would otherwise run
the full pipeline and persist another user message + AI reply. The first
request claims the turn here; a re-request finds the claim and replays the
first one's outcome instead of re-running the pipeline.

Every operation FAILS OPEN: a Redis problem means "no dedupe" (the old
behaviour), never a failed or silent turn.
"""

import asyncio
import hashlib
import json
from typing import Any
from uuid import UUID

import redis.asyncio as aioredis
from loguru import logger

from app.core.config import settings

_KEY_PREFIX = "voice:turn:"
_PENDING = "pending"
# Long enough to cover EL's abandon/re-request churn, short enough that a
# caller genuinely repeating themselves later is never mistaken for a retry.
_TTL_SECONDS = 60
_POLL_INTERVAL_SECONDS = 0.1


def turn_key(session_id: UUID, history_len: int, user_text: str) -> str:
    """Same session + same history length + same final user text == same turn.

    `history_len` is what separates a retry (identical history) from the caller
    legitimately saying the same words again later (longer history).
    """
    digest = hashlib.sha256(f"{session_id}|{history_len}|{user_text}".encode()).hexdigest()
    return f"{_KEY_PREFIX}{digest}"


async def claim(key: str) -> bool:
    """True when this request owns the turn (or dedupe is unavailable)."""
    client = None
    try:
        client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
        return bool(await client.set(key, _PENDING, nx=True, ex=_TTL_SECONDS))
    except Exception as exc:  # noqa: BLE001 -- fail open
        logger.warning(f"Voice turn dedupe claim failed open: {exc}")
        return True
    finally:
        if client is not None:
            await _close(client)


async def store(key: str, outcome: dict[str, Any]) -> None:
    client = None
    try:
        client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
        await client.set(key, json.dumps(outcome), ex=_TTL_SECONDS)
    except Exception as exc:  # noqa: BLE001 -- fail open
        logger.warning(f"Voice turn dedupe store failed: {exc}")
    finally:
        if client is not None:
            await _close(client)


async def release(key: str) -> None:
    """Drop the claim so a retry re-runs the turn (used when it didn't complete)."""
    client = None
    try:
        client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
        await client.delete(key)
    except Exception as exc:  # noqa: BLE001 -- fail open
        logger.warning(f"Voice turn dedupe release failed: {exc}")
    finally:
        if client is not None:
            await _close(client)


async def wait_for_outcome(key: str, *, timeout: float) -> dict[str, Any] | None:
    """Poll for the owning request's outcome; None on timeout/release/error."""
    client = None
    try:
        client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            raw = await client.get(key)
            if raw is None:
                return None
            if raw != _PENDING:
                outcome = json.loads(raw)
                return outcome if isinstance(outcome, dict) else None
            if loop.time() >= deadline:
                return None
            await asyncio.sleep(_POLL_INTERVAL_SECONDS)
    except Exception as exc:  # noqa: BLE001 -- fail open
        logger.warning(f"Voice turn dedupe wait failed: {exc}")
        return None
    finally:
        if client is not None:
            await _close(client)


async def _close(client: aioredis.Redis) -> None:
    try:
        await client.aclose()
    except Exception:  # noqa: BLE001 -- closing must never fail a turn
        logger.debug("Voice turn dedupe client close failed")
