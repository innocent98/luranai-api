"""Voice chat endpoints — ElevenLabs Agents integration.

Spec: docs/superpowers/specs/2026-08-15-voice-chat-backend-design.md
"""

import asyncio
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from loguru import logger
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.api.v1.endpoints.chat import send_message
from app.core.config import settings
from app.db.models.conversation import ConversationSession
from app.schemas.conversation import MessageRequest
from app.schemas.ticket import TicketCreate
from app.schemas.voice import VoiceTokenRequest, VoiceTokenResponse
from app.services.conversation_service import ConversationService
from app.services.ticket_service import TicketService
from app.services.voice.auth import verify_elevenlabs_request
from app.services.voice.elevenlabs_client import ElevenLabsClient, ElevenLabsMintError
from app.services.voice.gating import VoiceDenialReason, VoiceGate
from app.services.voice.session_tokens import VoiceSessionContext, VoiceSessionTokenStore
from app.services.voice.sse import stream_reply
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
            # `dynamic_variables` isn't actually transmitted yet — see the
            # TODO(spike-1) on ElevenLabsClient.mint_conversation_token.
            conversation_token = await ElevenLabsClient().mint_conversation_token(
                agent_id=settings.ELEVENLABS_AGENT_ID,
                dynamic_variables={"voice_token": voice_token},
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


def _extract_voice_token(payload: dict[str, Any]) -> str:
    """Pull our opaque token out of wherever ElevenLabs put it.

    We check several carriers because Spike 1 has not yet pinned down which
    one EL actually uses in production. All of them are opaque-token
    carriers, never trusted ids — the token is only ever meaningful after
    `VoiceSessionTokenStore.resolve` looks it up server-side.
    """
    extra = payload.get("elevenlabs_extra_body") or {}
    if isinstance(extra, dict) and extra.get("voice_token"):
        return str(extra["voice_token"])

    variables = payload.get("dynamic_variables") or {}
    if isinstance(variables, dict) and variables.get("voice_token"):
        return str(variables["voice_token"])

    if payload.get("user"):
        return str(payload["user"])

    return ""


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


def _speak(reply_text: str, *, model: str) -> StreamingResponse:
    """Materialise the SSE frames before responding: the reply is already
    complete by this point, so there is nothing left to stream progressively,
    and this keeps `db` from being touched after the request scope closes.
    Becomes a true async generator if/when the pipeline itself grows a
    streaming seam."""
    return StreamingResponse(
        iter(list(stream_reply(reply_text, model=model))),
        media_type="text/event-stream",
    )


@router.post("/llm/chat/completions", dependencies=[Depends(verify_elevenlabs_request)])
async def voice_custom_llm(
    payload: dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
):
    """OpenAI-compatible custom-LLM endpoint backed by the real Luran pipeline.

    ElevenLabs Agents calls this once per conversational turn as its "custom
    LLM". Parity with text chat is by construction: this calls the exact same
    `send_message` that `POST /chat/message` calls, so there is no second
    brain to keep in sync — there is only one pipeline.

    Nothing in this codebase streams (see spec's "Spec Corrections" #1), so
    this awaits the finished reply and re-frames it as OpenAI SSE via
    `stream_reply` rather than streaming the pipeline itself. Every failure
    mode on this endpoint — token-resolution errors, a throttled turn, a
    pipeline exception, a terminal policy rejection, an empty completion —
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
            logger.warning("Voice custom-LLM call carried an unresolvable voice token")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="unknown voice session",
            )
        user_text = _last_user_message(payload)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 — never leave a caller in silence
        logger.exception(f"Voice request setup failed before reaching the pipeline: {exc}")
        return _speak(_FALLBACK_REPLY, model=model)

    if not await _turn_allowed(token):
        logger.warning(f"Voice turn rate limit exceeded for session {context.luran_session_id}")
        return _speak(_RATE_LIMITED_REPLY, model=model)

    if not user_text:
        return _speak(_NO_INPUT_REPLY, model=model)

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
        return _speak(_TERMINAL_REPLY, model=model)
    except Exception as exc:  # noqa: BLE001 — never leave a caller in silence
        logger.exception(f"Voice pipeline failed for session {context.luran_session_id}: {exc}")
        reply_text = _FALLBACK_REPLY

    # The pipeline can succeed and still return nothing speakable (e.g. an
    # empty completion). That is not an error, so it gets its own fallback
    # rather than reusing `_FALLBACK_REPLY`'s "I ran into a problem" framing.
    if not reply_text or not reply_text.strip():
        reply_text = _EMPTY_REPLY

    return _speak(reply_text, model=model)


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
        existing = ticket_service.get_open_ticket_for_session(context.luran_session_id)
        if existing is not None:
            return {"switch_to_text": True, "ticket_id": str(existing.id)}

        # TODO(agent-caller): ring the on-call agent app here once it exists
        # (see project_agent_caller_app in memory) — today this only opens a
        # ticket for a human to pick up asynchronously.
        ConversationService(db).escalate_session(context.luran_session_id, reason)
        ticket = ticket_service.create_ticket(
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
