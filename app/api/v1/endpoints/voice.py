"""Voice chat endpoints — ElevenLabs Agents integration.

Spec: docs/superpowers/specs/2026-08-15-voice-chat-backend-design.md
"""

import asyncio
import re
from typing import Any
from uuid import UUID

from collections.abc import AsyncIterator

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from loguru import logger
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.api.v1.endpoints.chat import send_message
from app.core.config import settings
from app.utils.customer_credential import optional_customer, session_credential_matches
from app.db.models.conversation import ConversationSession, MessageRole
from app.db.session import SessionLocal
from app.schemas.conversation import MessageRequest
from app.schemas.ticket import TicketCreate
from app.schemas.voice import (
    VoiceSessionEndRequest,
    VoiceSessionEndResponse,
    VoiceTokenRequest,
    VoiceTokenResponse,
)
from app.services.conversation_service import ConversationService
from app.services.rag_service import RAGService
from app.services.ticket_service import TicketService
from app.services.voice.auth import verify_elevenlabs_request
from app.services.voice.elevenlabs_client import ElevenLabsClient, ElevenLabsMintError
from app.services.voice.gating import VoiceDenialReason, VoiceGate
from app.services.voice.session_tokens import VoiceSessionContext, VoiceSessionTokenStore
from app.services.voice import turn_dedupe, turn_signal
from app.services.voice.sse import SSEFramer, stream_reply
from app.utils.http_rate_limiter import voice_llm_turn_rate_limiter, voice_rate_limit

router = APIRouter()

# Spoken when the pipeline raises. Silence is the worst possible failure mode
# on a voice call, so we always return a speakable 200 rather than an error
# status — ElevenLabs has no way to render a non-200 as audio.
_FALLBACK_REPLY = "I am sorry, I ran into a problem just then. Could you say that again?"

# Spoken when we never got a usable transcript to send to the pipeline at all
# (e.g. ElevenLabs sent a turn with no user-role content).
_NO_INPUT_REPLY = "Sorry, I did not catch that. Could you say that again?"

# Spoken when the pipeline *succeeded* but the reply has no speakable content.
# This is deliberately different phrasing from `_FALLBACK_REPLY`: this is not
# an error, so telling the caller "I ran into a problem" would be wrong. This
# is the pipeline shrugging, not the pipeline breaking.
_EMPTY_REPLY = "I'm not sure how to help with that. Could you try asking a different way?"

# Spoken when the pipeline rejects the request for a reason that will never
# change on retry — an entitlement/existence HTTPException such as 402
# conversation-limit-reached or 404 session-not-found. Deliberately terminal
# phrasing, not "could you say that again?": inviting a retry here would trap
# the caller in a loop that can never succeed while burning ElevenLabs
# minutes every turn. The voice token is also revoked when this fires (see
# `_revoke_voice_token_best_effort`) so the NEXT turn 401s cleanly instead of
# repeating the same rejection — that ends OUR ability to answer, NOT the
# call itself: the WebRTC session stays open (and billing) until the caller
# or widget disconnects. Releasing the concurrency slot on disconnect is
# Task 11's, not this endpoint's.
_TERMINAL_REPLY = (
    "I'm sorry, but I'm not able to continue this call right now. "
    "Please check your account or contact support. Goodbye."
)

# Spoken when a single voice token has exceeded its per-minute turn budget.
# Transient, unlike `_TERMINAL_REPLY` — no token revoke, the caller just
# needs to slow down.
_RATE_LIMITED_REPLY = "Sorry, one moment please."

# Must exactly match the tool name declared in the ElevenLabs Agent config's
# `escalate_to_human` CLIENT tool (see `voice_escalate_to_human` below,
# `POST /tools/escalate_to_human`) -- ElevenLabs matches an emitted
# tool_calls[].function.name against its own tool inventory by exact
# string, so this cannot silently drift from that route's name.
_ESCALATE_TOOL_NAME = "escalate_to_human"

# Spoken when ElevenLabs calls this endpoint with the escalate_to_human
# CLIENT tool's RESULT as the next turn (`messages[-1].role == "tool"`) --
# see `_is_tool_result_turn`. That turn carries no new caller speech to
# feed the pipeline: the escalation itself already happened via the
# browser invoking `POST /tools/escalate_to_human` directly (that route
# marks the session escalated and creates/reuses the ticket), so re-running
# the pipeline here would have nothing new to send it, and re-escalating
# would risk a second ticket for a caller who has already been handed off.
# Deliberately NOT `_NO_INPUT_REPLY`'s wording ("I did not catch that") --
# this isn't a failed-ASR turn, it's EL reporting a tool result back to us,
# and telling the caller we didn't catch something they never said would be
# confusing right after they asked for a human.
# Spoken ONLY when the escalate client tool already ran earlier in this call
# (a `tool` message is in EL's history) and the caller keeps talking: a short,
# non-echoing pointer to the text chat. Never used for the first handoff, which
# must stay silent (see `_stream_pipeline_turn`).
_ESCALATED_CHAT_REPLY = "A teammate will continue with you in the chat."
_ESCALATION_ACK_REPLY = "Thanks for waiting. I'm connecting you with a member of our team now."

# Budget for RedisRateLimiter.acquire() inside `_turn_allowed`. Its own
# internal loop can `asyncio.sleep` up to `window_seconds + 1` (61s for
# `voice_llm_turn_rate_limiter`) if two concurrent turns on the same token
# both pass the fast usage check and then race for the same slot. A stuck
# acquire is worse than a false-positive throttle on the endpoint whose
# whole purpose is preventing silence, so it gets cut short well before that.
_TURN_RATE_LIMIT_TIMEOUT_SECONDS = 2.0


async def _release_reserved_slot(
    gate: VoiceGate, store: VoiceSessionTokenStore, tenant_id: UUID, voice_token: str
) -> None:
    """Best-effort cleanup of an atomically-reserved slot + its voice token.

    Both steps run even if one of them raises — a Redis hiccup releasing
    the slot must not skip revoking the token, or vice versa. Failures are
    logged, not re-raised: this runs from a `finally` block, where a new
    exception would silently replace whatever error the caller was already
    propagating (e.g. the intended 503). A slot left behind by a failure
    here still self-heals via the concurrency key's per-member TTL; the
    voice token still self-heals via its own Redis TTL.
    """
    try:
        await gate.release_slot(tenant_id, voice_token)
    except RedisError as exc:
        logger.error(f"Failed to release voice concurrency slot for tenant {tenant_id}: {exc}")
    try:
        await store.revoke(voice_token)
    except RedisError as exc:
        logger.error(f"Failed to revoke voice token for tenant {tenant_id}: {exc}")


@router.post(
    "/session/token",
    response_model=VoiceTokenResponse,
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(voice_rate_limit)],
)
async def mint_voice_session_token(
    payload: VoiceTokenRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """Mint a short-lived ElevenLabs WebRTC token, subject to tenant gating.

    409 with a machine-readable reason means the widget should stay in text.
    A 503 (unconfigured provider or infra outage) means the same, but is our
    fault rather than a policy decision.
    """
    if not settings.ELEVENLABS_AGENT_ID:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"reason": "voice_not_configured"},
        )

    # The session must exist AND belong to this tenant — this is the
    # tenant-isolation boundary. mint() below trusts whatever tenant_id its
    # caller passes, by design, so the check lives here.
    session = db.execute(
        select(ConversationSession).where(
            ConversationSession.id == payload.luran_session_id,
            ConversationSession.tenant_id == payload.tenant_id,
        )
    ).scalar_one_or_none()
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"reason": "session_not_found"},
        )

    # The minted token later writes USER messages through the trusted send_message path,
    # so minting needs the same customer credential as the public chat message route.
    if not session_credential_matches(
        db,
        session,
        user_identifier=payload.user_identifier,
        visitor_token=payload.visitor_token,
        customer=optional_customer(request, db),
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"reason": "invalid_customer_credential"},
        )

    gate = VoiceGate(db)
    store = VoiceSessionTokenStore()

    try:
        result = await gate.check(payload.tenant_id)
        # check()'s own concurrency step is a non-atomic peek (see gating.py);
        # it's a useful fast-fail for flag/cap, but the concurrency dimension
        # is decided authoritatively below by the atomic reserve, not here.
        if not result.allowed and result.reason != VoiceDenialReason.CONCURRENCY_LIMIT:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"reason": result.reason.value},
            )

        voice_token = await store.mint(
            tenant_id=payload.tenant_id,
            luran_session_id=payload.luran_session_id,
            text_out=payload.text_out,
        )

        acquired = await gate.try_acquire_slot(payload.tenant_id, voice_token)
        if not acquired:
            await store.revoke(voice_token)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"reason": VoiceDenialReason.CONCURRENCY_LIMIT.value},
            )

        el_succeeded = False
        try:
            # `participant_name` is the confirmed (Spike 1) carrier for our
            # opaque voice_token — see ElevenLabsClient.mint_conversation_token's
            # docstring for the full mint -> JWT -> prompt-substitution chain.
            conversation_token = await ElevenLabsClient().mint_conversation_token(
                agent_id=settings.ELEVENLABS_AGENT_ID,
                participant_name=voice_token,
            )
            el_succeeded = True
        except ElevenLabsMintError as exc:
            logger.error(f"Voice token mint failed: {exc}")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"reason": "voice_provider_unavailable"},
            ) from exc
        except Exception as exc:
            # Anything else surfacing from the EL call (a malformed response
            # body the client didn't already normalize, etc.) is still our
            # fault, not the caller's — same controlled fallback, not a 500.
            logger.error(f"Unexpected error minting ElevenLabs conversation token: {exc}")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"reason": "voice_provider_unavailable"},
            ) from exc
        finally:
            # Runs on every non-success exit from the block above —
            # ElevenLabsMintError, the broad Exception catch, and even a
            # BaseException like asyncio.CancelledError on client disconnect
            # (a `finally` always runs; a bare `except` would not have
            # caught that last one). Don't leave an orphaned voice-token
            # binding or a leaked concurrency slot behind for a mint that
            # never happened.
            if not el_succeeded:
                await _release_reserved_slot(gate, store, payload.tenant_id, voice_token)
    except HTTPException:
        raise
    except RedisError as exc:
        # Neither VoiceGate nor VoiceSessionTokenStore wraps its Redis calls,
        # so an outage would otherwise propagate as an unhandled 500. Turn it
        # into the same controlled fallback signal the widget already
        # understands rather than letting it 500.
        logger.error(f"Voice session infra error during mint: {exc}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"reason": "voice_provider_unavailable"},
        ) from exc

    return VoiceTokenResponse(
        conversation_token=conversation_token,
        agent_id=settings.ELEVENLABS_AGENT_ID,
        voice_token=voice_token,
        expires_in_seconds=settings.VOICE_SESSION_TOKEN_TTL_SECONDS,
    )


@router.post(
    "/session/end",
    response_model=VoiceSessionEndResponse,
    status_code=status.HTTP_200_OK,
)
async def end_voice_session(
    payload: VoiceSessionEndRequest,
    db: Session = Depends(get_db),
):
    """Called by the widget on hang-up: bill the call's final partial
    minute and free its concurrency slot.

    Metering is NOT primarily enforced here. `VoiceSessionTokenStore.resolve()`
    meters incrementally on every conversational turn -- each turn is proof
    the caller is still on the call -- so the minute cap has teeth mid-call
    and a widget that crashes, loses network, or is simply closed still gets
    billed for every turn it completed before that happened. This route only
    bills the small trailing delta between the last turn and hang-up (via
    `resolve_and_revoke`, see its docstring) and frees the concurrency slot;
    both best-effort, since nothing here renders a failure to anyone -- the
    widget has already torn the call down locally by the time this fires.

    Deliberately unauthenticated like `/session/token` -- the opaque voice
    token IS the credential, and possession of it is the only thing gating
    this route. This is NOT a self-healing no-op if misused: `resolve_and_revoke`
    deletes the token's binding immediately, so a call to this route against
    someone else's still-live token ends that real call early -- its very
    next turn 401s and goes silent for the caller who was still talking, not
    "recovers via a TTL" the way a merely leaked slot would. Exploitability
    is low in practice (the token is a 256-bit `secrets.token_urlsafe(32)`
    value that only ever appears in the mint response and ElevenLabs' own
    request), so this isn't asking for auth -- it's naming the actual blast
    radius rather than a self-heal property this route does not have.

    Idempotent by construction against duplicate/racing calls for the SAME
    token: `resolve_and_revoke` (Redis GETDEL) deletes the binding atomically
    as the first thing this does, so a second call for that token -- a
    duplicate hangup event, a client retry, a genuine race between two
    near-simultaneous end requests -- resolves to `None` and no-ops rather
    than metering or releasing twice.

    Slot release is wrapped in its own try/except so a failure there is
    logged and swallowed, never surfaced as a 500 -- a leaked slot here
    still ages out via the concurrency ZSET's own per-member TTL (2h, see
    gating.py). `resolve_and_revoke` already handles its own metering
    failures internally (see its docstring) without raising, so there is
    nothing further to guard between it and the slot release.
    """
    store = VoiceSessionTokenStore()
    try:
        context = await store.resolve_and_revoke(payload.voice_token)
    except Exception as exc:  # noqa: BLE001 -- never 500 on hangup
        logger.error(f"Voice session-end token resolution failed: {exc}")
        return VoiceSessionEndResponse()

    if context is None:
        # Unknown, already-ended, or expired token — nothing left to do,
        # and no information worth leaking about which of those it was.
        return VoiceSessionEndResponse()

    try:
        await VoiceGate(db).release_slot(context.tenant_id, payload.voice_token)
    except Exception as exc:  # noqa: BLE001 -- a leaked slot self-heals via its own TTL
        logger.error(
            f"Failed to release voice concurrency slot for tenant {context.tenant_id}: {exc}"
        )

    return VoiceSessionEndResponse()


# Our minted tokens are always `secrets.token_urlsafe(32)` -- base64 of 32
# random bytes, URL-safe alphabet, stripped of padding. 32 bytes % 3 == 2,
# so base64 always emits exactly 44 chars with one '=' pad char, which
# `token_urlsafe` strips -- the length is therefore a CONSTANT, not merely
# a typical value, and validating it exactly (not ">= some minimum") is
# what makes this fail closed rather than accepting arbitrary trailing text
# after "Session:" (e.g. the unsubstituted literal "{{voice_token}}", or a
# KB document that happens to contain the word "Session:").
_VOICE_TOKEN_LENGTH = 43
_VOICE_TOKEN_ALPHABET = "A-Za-z0-9_-"
# `[ \t]*` NOT `\s*`: `\s` crosses newlines, so a prompt whose operator wrote
# "Session:" but forgot "{{voice_token}}" would happily bind to whatever
# token-shaped word began the NEXT line. Same-line only.
_SESSION_TOKEN_RE = re.compile(
    rf"Session:[ \t]*([{_VOICE_TOKEN_ALPHABET}]{{{_VOICE_TOKEN_LENGTH}}})(?![{_VOICE_TOKEN_ALPHABET}])"
)


def _extract_voice_token(payload: dict[str, Any]) -> str:
    """Pull our opaque token out of `messages[0].content` -- the ElevenLabs
    Agent's system prompt, after EL's own `{{voice_token}}` template
    substitution.

    Confirmed live in Spike 1 (2026-08-19, 28 captured requests against a
    real throwaway ElevenLabs Agent): the custom-LLM request body contains
    EXACTLY `messages`, `model`, `max_tokens`, `stream`, `stream_options`,
    `temperature`, `tools` -- there is no `elevenlabs_extra_body`, no
    `dynamic_variables`, no `user`, no `conversation_id`, no `agent_id`. The
    three carriers this function used to check never existed on the wire.

    The real mechanism: `ElevenLabsClient.mint_conversation_token` sends our
    voice_token as the mint call's `participant_name` query param; EL binds
    that into the session JWT as `sub`; the Agent's (operator-configured)
    system prompt contains the literal template text
    `Session: {{voice_token}}`; EL substitutes `{{voice_token}}` with the
    JWT's bound value when building the prompt; that substituted prompt is
    what arrives here as `messages[0].content`.

    NOT line-anchored: the real captured shape has "Session:" embedded
    mid-string, preceded by "Agent character description: " on the same
    line, and followed by a newline plus indentation before more prompt
    text continues -- `^Session:` would never match it.

    Fails closed (returns "", which `VoiceSessionTokenStore.resolve()`
    treats as unconditionally unresolvable -> 401) whenever the shape isn't
    exactly right: no `messages[0]`, non-dict `messages[0]`, non-string
    content, no `Session:` marker at all (operator never configured the
    prompt template), or a `Session:` marker not followed by a real
    43-char token (most notably the literal unsubstituted template text
    `{{voice_token}}` itself, if EL substitution didn't happen for some
    reason -- see `test_unsubstituted_template_placeholder_fails_closed`).
    Never returns a guess or a partial match.
    """
    messages = payload.get("messages") or []
    if not messages:
        return ""
    first = messages[0]
    if not isinstance(first, dict):
        return ""
    content = first.get("content")
    if not isinstance(content, str):
        return ""
    matches = _SESSION_TOKEN_RE.findall(content)
    # AMBIGUITY FAILS CLOSED. `search()` would silently take the FIRST match,
    # so any second `Session: <43-char>` appearing earlier in the prompt --
    # a knowledge-base document, a pasted transcript, an operator's own
    # example text -- would decide which tenant we resolve. Today the real
    # marker happens to come first (EL puts the persona above the KB), so
    # first-match is correct by accident of their prompt assembly, not by
    # design. Requiring exactly one match removes the dependence on that
    # ordering: two candidates is an unanswerable question, so we 401 rather
    # than guess. (A forged token still has to resolve in our own Redis, so
    # this is defence in depth, not the only barrier.)
    if len(matches) != 1:
        return ""
    return matches[0]


# What follows "Session:" on the same line, up to whitespace -- used only to
# report its LENGTH, never its value.
_SESSION_MARKER_VALUE_RE = re.compile(r"Session:[ \t]*(\S*)")


def _describe_voice_token_shape(payload: dict[str, Any]) -> dict[str, Any]:
    """Shape-only diagnostics for an unresolvable voice token.

    Separates the failure modes that all end in the same 401 -- no marker,
    EL left `{{voice_token}}` unsubstituted, a wrong-length value, two
    candidates (ambiguous), or a token that parsed fine but isn't in Redis
    (`extracted=True`) -- without ever returning the token, any fragment of
    it, or any prompt text. Safe to log.
    """
    messages = payload.get("messages") or []
    shape: dict[str, Any] = {
        "messages": len(messages) if isinstance(messages, list) else None,
        "first_message_type": None,
        "first_role": None,
        "content_len": None,
        "has_session_marker": False,
        "has_unsubstituted_template": False,
        "marker_value_len": None,
        "token_candidates": 0,
        "extracted": False,
    }
    if not messages or not isinstance(messages, list):
        return shape
    first = messages[0]
    shape["first_message_type"] = type(first).__name__
    if not isinstance(first, dict):
        return shape
    shape["first_role"] = first.get("role")
    content = first.get("content")
    if not isinstance(content, str):
        return shape
    shape["content_len"] = len(content)
    shape["has_session_marker"] = "Session:" in content
    shape["has_unsubstituted_template"] = "{{voice_token}}" in content
    marker = _SESSION_MARKER_VALUE_RE.search(content)
    if marker:
        shape["marker_value_len"] = len(marker.group(1))
    shape["token_candidates"] = len(_SESSION_TOKEN_RE.findall(content))
    shape["extracted"] = bool(_extract_voice_token(payload))
    return shape


def _last_user_message(payload: dict[str, Any]) -> str:
    """Extract the caller's current turn — and only the current turn.

    Only `messages[-1]` counts. An earlier draft of this walked backward past
    an empty/non-user last entry looking for the most recent user turn — but
    a failed-ASR or silent turn (content == "") is a real, distinct event: it
    means the caller said nothing usable *this* turn. Falling back to an
    earlier turn would silently re-drive the pipeline with stale text,
    re-running lead scoring and the action engine, and speak a stale answer
    back to a caller who said nothing new. Any non-`user`, missing, or
    non-dict last entry is "no input", never "look further back". Whitespace-
    only content (a realistic partial-ASR artifact — e.g. a single space) is
    "no input" too: it's truthy in Python, so without stripping it would
    clear the caller's no-input gate and reach the pipeline with nothing
    actually said.
    """
    messages = payload.get("messages") or []
    if not messages:
        return ""
    last = messages[-1]
    if not isinstance(last, dict) or last.get("role") != "user":
        return ""
    content = last.get("content")
    if not content:
        return ""
    return str(content).strip()


def _is_tool_result_turn(payload: dict[str, Any]) -> bool:
    """True when EL's current turn is the tool-RESULT callback that follows
    a tool_calls turn, not a fresh caller utterance.

    OpenAI shape: `{"role": "tool", "tool_call_id": ..., "content": ...}`
    appended as the new `messages[-1]` on the NEXT custom-LLM call after we
    emitted a tool_calls delta (see `_ESCALATE_TOOL_NAME` usage below).
    Deliberately only checks the role, not `tool_call_id` or `content`
    shape -- any malformed/unexpected extra fields on a genuine tool-result
    message must still be recognized as one, not fall through to
    `_last_user_message` and speak the wrong ("did not catch that")
    fallback. Mirrors `_last_user_message`'s own malformed-last-message
    guard: non-dict or missing `messages` -> False, never a crash.
    """
    messages = payload.get("messages") or []
    if not messages:
        return False
    last = messages[-1]
    return isinstance(last, dict) and last.get("role") == "tool"


def _tool_already_invoked(payload: dict[str, Any]) -> bool:
    """True when EL's history already holds a tool RESULT, i.e. the escalate
    client tool ran earlier in this call (stateless: no server-side flag)."""
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return False
    return any(isinstance(m, dict) and m.get("role") == "tool" for m in messages)


async def _revoke_voice_token_best_effort(token: str, tenant_id: UUID) -> None:
    """Best-effort revoke after a terminal pipeline rejection.

    This ends OUR ability to answer further turns on this token — the next
    `resolve()` 401s instead of repeating the same unfixable rejection — but
    NOT the call itself: the WebRTC session stays open and keeps burning
    ElevenLabs minutes until the caller or widget disconnects. Ending the
    call itself, and releasing the concurrency slot on disconnect, is out of
    scope here (Task 11's).

    Mirrors `_release_reserved_slot`'s pattern above: failures are logged,
    not re-raised — we're already mid-response with a reply ready to send,
    and a Redis hiccup here still self-heals via the token's own TTL.
    """
    try:
        await VoiceSessionTokenStore().revoke(token)
    except RedisError as exc:
        logger.error(
            f"Failed to revoke voice token for tenant {tenant_id} after pipeline rejection: {exc}"
        )


async def _turn_allowed(token: str) -> bool:
    """Per-voice-token turn throttle. Returns False only when over budget.

    Keyed by the resolved voice token, not IP or user: ElevenLabs calls this
    endpoint server-to-server for every tenant's every turn, almost certainly
    from a small, shared pool of egress IPs, so an IP-keyed limit (like
    `voice_rate_limit` on `/session/token`) would throttle every unrelated
    tenant's call sharing that IP together. Fails OPEN on any infra error —
    a Redis hiccup must never turn into a throttled (and therefore silent-
    feeling) turn on this path.
    """
    try:
        usage = await voice_llm_turn_rate_limiter.get_current_usage(token)
        if usage["calls"] >= voice_llm_turn_rate_limiter.max_calls:
            return False
        await asyncio.wait_for(
            voice_llm_turn_rate_limiter.acquire(token),
            timeout=_TURN_RATE_LIMIT_TIMEOUT_SECONDS,
        )
        return True
    except TimeoutError:
        # `acquire()` re-checked the window itself (a second, later read than
        # the `get_current_usage` above) and is stuck in its own sleep-and-
        # retry loop — almost certainly a race with a concurrent turn on the
        # same token. Treat "stuck" the same as "over budget": throttle this
        # turn rather than hang the request for up to 61s.
        logger.warning("Voice turn rate limiter acquire() timed out; throttling this turn")
        return False
    except Exception as exc:  # noqa: BLE001 — fail open, infra errors must not throttle
        logger.error(f"Voice turn rate limiter failed open: {exc}")
        return True


# `X-Accel-Buffering: no` stops nginx holding SSE frames in its proxy buffer
# (the buffer word is worthless if it sits in nginx for the whole pipeline);
# `no-cache` stops any intermediary caching/reshaping the stream.
_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}

# The buffer word is spoken CONTENT, and a tool-call turn must not be preceded
# by speech (ElevenLabs does not invoke the client tool after content; seen
# live). The pipeline decides escalations at many points, some slower than any
# fixed grace period, so instead of a timer the buffer waits for
# `turn_signal.generation_started()`, which chat.py fires right before the LLM
# call: only turns that really generate get filler. A turn that finishes
# without the signal (escalation / early-return branch) gets no buffer at all.
#
# Safety cap for the rare path where the signal never arrives while the
# pipeline is still running (e.g. a slow flow/automation step that ends in a
# long reply, or a duplicate turn replaying a slow original). 2.5 s: well above
# the normal retrieval time before generation (~0.5-1 s), so ordinary turns are
# signalled first and a late escalation is not preceded by filler; yet short
# enough that the caller is not left in silence for much longer than the live
# EL patience (~a few seconds) on a turn that is just slow. On this path a
# late escalation CAN still get the buffer word ahead of its tool call.
_BUFFER_MAX_WAIT_SECONDS = 2.5
# How long a duplicate (re-issued) request waits for the original's outcome.
_DUPLICATE_WAIT_SECONDS = 10.0

# Strong refs so a pipeline task that outlives an abandoned request is not GC'd.
_background_turns: set[asyncio.Task] = set()


def _sse_response(frames) -> StreamingResponse:
    return StreamingResponse(frames, media_type="text/event-stream", headers=_SSE_HEADERS)


def _speak(reply_text: str, *, model: str) -> StreamingResponse:
    """Stream a ready-made reply (no pipeline involved) as SSE frames."""
    return _sse_response(iter(list(stream_reply(reply_text, model=model))))


class _TurnOutcome:
    """What the pipeline decided: spoken text, or the escalate tool call."""

    __slots__ = ("text", "tool_name")

    # `text` together with `tool_name` = speak the line, then emit the tool call.
    def __init__(self, *, text: str = "", tool_name: str | None = None) -> None:
        self.text = text
        self.tool_name = tool_name


def _open_stream_session() -> Session:
    """A session owned by the streaming task, not the request.

    FastAPI (0.115, pinned) exits yield-dependencies -- closing the request's
    `db` -- BEFORE a StreamingResponse body runs. Touching that closed Session
    would silently check out a fresh pooled connection nobody ever closes (one
    leak per voice turn). The task opens and closes its own instead."""
    return SessionLocal()


async def _run_turn(
    *,
    token: str,
    context: VoiceSessionContext,
    user_text: str,
    dedupe_key: str,
    tool_already_invoked: bool = False,
) -> _TurnOutcome:
    """Run one caller turn through the pipeline. Never raises: every failure
    mode becomes speakable text (or the escalation tool call)."""
    if not await turn_dedupe.claim(dedupe_key):
        # EL re-issued a turn we are already (or have already finished)
        # handling. Replay the original outcome rather than persisting a second
        # user message + AI reply.
        logger.info(f"Voice duplicate turn for session {context.luran_session_id}; replaying")
        replay = await turn_dedupe.wait_for_outcome(dedupe_key, timeout=_DUPLICATE_WAIT_SECONDS)
        if replay is not None:
            return _TurnOutcome(
                text=str(replay.get("text") or ""), tool_name=replay.get("tool_name")
            )
        return _TurnOutcome(text=_FALLBACK_REPLY)

    db = _open_stream_session()
    try:
        outcome = await _run_pipeline(
            db=db,
            token=token,
            context=context,
            user_text=user_text,
            tool_already_invoked=tool_already_invoked,
        )
    finally:
        db.close()
    if outcome.text == _FALLBACK_REPLY and outcome.tool_name is None:
        # Pipeline errored: let a retry genuinely re-run instead of replaying it.
        await turn_dedupe.release(dedupe_key)
    else:
        await turn_dedupe.store(dedupe_key, {"text": outcome.text, "tool_name": outcome.tool_name})
    return outcome


async def _run_turn_signalled(started: asyncio.Event, **kwargs: Any) -> _TurnOutcome:
    """`_run_turn` with the generation signal bound inside the task's own
    context, so concurrent turns never share an event."""
    turn_signal.bind(started)
    return await _run_turn(**kwargs)


async def _run_pipeline(
    *,
    db: Session,
    token: str,
    context: VoiceSessionContext,
    user_text: str,
    tool_already_invoked: bool = False,
) -> _TurnOutcome:
    try:
        chat_response = await send_message(
            MessageRequest(
                message=user_text,
                session_id=context.luran_session_id,
                tenant_id=context.tenant_id,
            ),
            db,
        )
        reply_text = chat_response.message.content
        if chat_response.message.role == MessageRole.USER:
            # ALREADY-ESCALATED session: `send_message` skips generation and
            # echoes the persisted USER message (chat.py "Skipping AI
            # generation"). Speaking that back plays the caller's own words to
            # them. A human owns the thread, so hand the widget back to text.
            logger.info(f"Voice turn on escalated session {context.luran_session_id}; switching")
            return _TurnOutcome(
                text=_ESCALATED_CHAT_REPLY if tool_already_invoked else "",
                tool_name=_ESCALATE_TOOL_NAME,
            )
        if chat_response.should_escalate:
            # The ESCALATION DECISION STAYS IN OUR PIPELINE. ElevenLabs hands
            # `escalate_to_human` to us in `tools` and only invokes the browser
            # client tool once we emit a tool_calls delta -- so before this
            # branch existed the tool could never fire, no matter what the
            # caller said (confirmed live in Spike 1: a caller asked for a
            # human twice and nothing happened).
            #
            # Emit ONLY the tool call, not spoken text: EL speaks whatever the
            # client tool returns, so adding content here would talk over it.
            # (A slow turn may already have sent the buffer word "... " -- see
            # `_BUFFER_MAX_WAIT_SECONDS`; nothing else is spoken.)
            # The follow-up tool-RESULT turn is caught by
            # `_is_tool_result_turn` and answered with `_ESCALATION_ACK_REPLY`,
            # which is why this does not re-run the pipeline or re-escalate.
            logger.info(
                f"Voice escalating session {context.luran_session_id}: "
                f"{chat_response.escalation_reason or 'no reason given'}"
            )
            return _TurnOutcome(tool_name=_ESCALATE_TOOL_NAME)
    except HTTPException as exc:
        # send_message's own HTTPExceptions are policy/entitlement or
        # existence errors (e.g. 402 conversation-limit-reached, 404 session
        # not found) — not transient. Retrying with the same words can never
        # succeed, so falling back to `_FALLBACK_REPLY`'s "could you say that
        # again?" would trap the caller in an infinite loop burning
        # ElevenLabs minutes. Revoke so the next turn 401s cleanly instead —
        # that ends our ability to answer, not the WebRTC call itself.
        logger.warning(f"Voice pipeline rejected session {context.luran_session_id}: {exc.detail}")
        await _revoke_voice_token_best_effort(token, context.tenant_id)
        return _TurnOutcome(text=_TERMINAL_REPLY)
    except Exception as exc:  # noqa: BLE001 — never leave a caller in silence
        logger.exception(f"Voice pipeline failed for session {context.luran_session_id}: {exc}")
        return _TurnOutcome(text=_FALLBACK_REPLY)

    # The pipeline can succeed and still return nothing speakable (e.g. an
    # empty completion). That is not an error, so it gets its own fallback
    # rather than reusing `_FALLBACK_REPLY`'s "I ran into a problem" framing.
    if not reply_text or not reply_text.strip():
        reply_text = _EMPTY_REPLY
    return _TurnOutcome(text=reply_text)


async def _stream_pipeline_turn(
    *,
    request: Request,
    model: str,
    token: str,
    context: VoiceSessionContext,
    user_text: str,
    dedupe_key: str,
    tool_already_invoked: bool = False,
) -> AsyncIterator[str]:
    """True streaming: role frame first (immediately), then the pipeline runs
    while the buffer word covers its latency, then the reply frames."""
    framer = SSEFramer(model=model)
    yield framer.role()

    # EL already gave up on this request (it re-requests when it thinks we are
    # slow): do not start a pipeline run nobody will hear.
    if await request.is_disconnected():
        logger.info(f"Voice request abandoned before pipeline start ({context.luran_session_id})")
        return

    # The pipeline runs as its own task so an abandoned request (generator
    # cancelled on client disconnect) does not tear a half-persisted turn
    # apart; its outcome lands in the dedupe cache for EL's re-request.
    started = asyncio.Event()
    task = asyncio.ensure_future(
        _run_turn_signalled(
            started,
            token=token,
            context=context,
            user_text=user_text,
            dedupe_key=dedupe_key,
            tool_already_invoked=tool_already_invoked,
        )
    )
    _background_turns.add(task)
    task.add_done_callback(_background_turns.discard)

    # A turn we already know will tool-call (the caller asked for a human, or
    # the handoff tool already ran) must not speak ahead of the tool call: EL's
    # behaviour for content followed by tool_calls is undocumented, and the
    # live failure was exactly that (buffer word, then the tool never fired).
    # Same cheap predicate `should_escalate` uses -- no second keyword list.
    silent_turn = tool_already_invoked or RAGService.is_explicit_human_request(user_text)

    if silent_turn:
        await asyncio.wait({task})
    else:
        # Buffer only once generation is about to start (or at the safety cap).
        signal_waiter = asyncio.ensure_future(started.wait())
        try:
            await asyncio.wait(
                {task, signal_waiter},
                timeout=_BUFFER_MAX_WAIT_SECONDS,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            signal_waiter.cancel()
        if not task.done():
            yield framer.buffer()
            await asyncio.wait({task})
    outcome = task.result()

    if outcome.tool_name is not None:
        # `text` (only the "already handed off" case) is spoken first.
        for frame in framer.words(outcome.text):
            yield frame
        for frame in framer.tool_call(outcome.tool_name):
            yield frame
        return

    for frame in framer.words(outcome.text):
        yield frame
    for frame in framer.finish():
        yield frame


@router.post("/llm/chat/completions", dependencies=[Depends(verify_elevenlabs_request)])
async def voice_custom_llm(
    request: Request,
    payload: dict[str, Any] = Body(...),
):
    """OpenAI-compatible custom-LLM endpoint backed by the real Luran pipeline.

    ElevenLabs Agents calls this once per conversational turn as its "custom
    LLM". Parity with text chat is by construction: this calls the exact same
    `send_message` that `POST /chat/message` calls, so there is no second
    brain to keep in sync — there is only one pipeline.

    The pipeline itself does not stream (see spec's "Spec Corrections" #1), but
    the RESPONSE does: the role frame is flushed immediately and the buffer
    word follows if the pipeline is slow, so ElevenLabs sees bytes within
    milliseconds instead of abandoning and re-requesting a slow turn. Every
    failure mode on this endpoint — token-resolution errors, a throttled turn,
    a pipeline exception, a terminal policy rejection, an empty completion —
    still ends in a 200 carrying something speakable. A non-200 or an empty
    stream leaves the caller in silence, which ElevenLabs has no way to
    render as audio. The one exception is authn/authz (missing shared secret,
    unresolvable token): those are allowed to be a real 401, since ElevenLabs
    itself is the caller for those failures, not the end user.
    """
    model = str(payload.get("model") or "luran-voice")

    # Tenant/session identity comes ONLY from resolving our own opaque voice
    # token server-side. Never from any EL-supplied id in the request body —
    # see spec §9 and the module docstring on VoiceSessionTokenStore. Errors
    # from extraction/resolution (a Redis outage, a malformed body) happen
    # before we know who the caller even is, so they get the generic
    # `_FALLBACK_REPLY`. The 401 raise must NOT be swallowed by that — the
    # `except HTTPException: raise` below is what keeps it a real 401 rather
    # than a masked 200.
    try:
        token = _extract_voice_token(payload)
        context: VoiceSessionContext | None = await VoiceSessionTokenStore().resolve(token)
        if context is None:
            logger.warning(
                "Voice custom-LLM call carried an unresolvable voice token "
                f"(shape={_describe_voice_token_shape(payload)})"
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="unknown voice session",
            )
        # A tool-result callback turn (see `_is_tool_result_turn`) carries
        # no new caller speech -- computed here, before `_last_user_message`,
        # so that turn never gets misread as "no input" by the generic
        # empty-content check below.
        tool_result_turn = _is_tool_result_turn(payload)
        user_text = "" if tool_result_turn else _last_user_message(payload)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 — never leave a caller in silence
        logger.exception(f"Voice request setup failed before reaching the pipeline: {exc}")
        return _speak(_FALLBACK_REPLY, model=model)

    if not await _turn_allowed(token):
        logger.warning(f"Voice turn rate limit exceeded for session {context.luran_session_id}")
        return _speak(_RATE_LIMITED_REPLY, model=model)

    if tool_result_turn:
        # The escalation itself already happened via the browser's direct
        # call to `POST /tools/escalate_to_human` -- nothing to re-run here,
        # and re-entering the pipeline with no real user text would either
        # no-input-fallback or (worse) persist a junk turn.
        return _speak(_ESCALATION_ACK_REPLY, model=model)

    if not user_text:
        return _speak(_NO_INPUT_REPLY, model=model)

    messages = payload.get("messages")
    dedupe_key = turn_dedupe.turn_key(
        context.luran_session_id, len(messages) if isinstance(messages, list) else 0, user_text
    )
    return _sse_response(
        _stream_pipeline_turn(
            request=request,
            model=model,
            token=token,
            context=context,
            user_text=user_text,
            dedupe_key=dedupe_key,
            tool_already_invoked=_tool_already_invoked(payload),
        )
    )


@router.post("/tools/escalate_to_human", dependencies=[Depends(verify_elevenlabs_request)])
async def voice_escalate_to_human(
    payload: dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
):
    """Voice -> human handoff client tool. Converts the call to text (spec §7).

    ElevenLabs invokes this client tool when the caller asks for a human.
    The tool name (`escalate_to_human`) is case-sensitive and must match the
    ElevenLabs Agent config exactly. The widget half (ending the EL session,
    switching the UI to text) is Task 8's; this is only the server half:
    mark the session escalated and create (or reuse) a ticket so a human can
    pick the conversation up.

    Tenant/session identity comes ONLY from resolving our own opaque voice
    token server-side, the same boundary `voice_custom_llm` holds above —
    never from any EL-supplied id in the request body.

    Idempotent by design: a caller saying "get me a person" more than once
    on the same session reuses the session's existing open ticket
    (`TicketService.get_open_ticket_for_session`) rather than opening a
    second one, and skips re-escalating a session that is already escalated.
    """
    try:
        context = await VoiceSessionTokenStore().resolve(str(payload.get("voice_token") or ""))
    except Exception as exc:  # noqa: BLE001 -- an infra hiccup here isn't the
        # caller's fault (unlike a genuinely unresolvable token below, which
        # stays a real 401) — mirrors the resolve-step fallback in
        # `voice_custom_llm` above rather than letting it surface as a 500.
        logger.error(f"Voice escalate-to-human token resolution failed: {exc}")
        return {"switch_to_text": True, "ticket_id": None}

    if context is None:
        logger.warning("Voice escalate-to-human call carried an unresolvable voice token")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="unknown voice session",
        )

    reason = str(payload.get("reason") or "Customer requested a human during a voice call")

    ticket_service = TicketService(db)

    try:
        # TODO(agent-caller): ring the on-call agent app here once it exists
        # (see project_agent_caller_app in memory) — today this only opens a
        # ticket for a human to pick up asynchronously.
        # escalate_session is a no-op on an already-escalated session, so a
        # repeated tool call neither restarts the clock nor re-escalates; it
        # commits, so it must run before the lock is taken.
        ConversationService(db).escalate_session(context.luran_session_id, reason)
        # Same per-session lock + live-ticket guard as chat escalation: a repeated
        # or concurrent tool call reuses the session's open ticket.
        ticket, _created = ticket_service.get_or_create_live_ticket(
            TicketCreate(
                tenant_id=context.tenant_id,
                session_id=context.luran_session_id,
                summary=reason,
            )
        )
    except Exception as exc:  # noqa: BLE001 -- never leave the caller in silence
        # A failure here (tenant ticket-creation capability disabled, the
        # session vanishing mid-call, a DB hiccup) must still let the widget
        # drop to text rather than surface a 500 that ElevenLabs cannot
        # render as speech. No ticket_id in that case: there is genuinely no
        # ticket for a human to pick up, but the caller isn't left hanging
        # on the call either.
        logger.error(f"Voice escalation failed for session {context.luran_session_id}: {exc}")
        return {"switch_to_text": True, "ticket_id": None}

    return {"switch_to_text": True, "ticket_id": str(ticket.id)}
