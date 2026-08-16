"""Voice chat endpoints — ElevenLabs Agents integration.

Spec: docs/superpowers/specs/2026-08-15-voice-chat-backend-design.md
"""

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
from app.schemas.voice import VoiceTokenRequest, VoiceTokenResponse
from app.services.voice.auth import verify_elevenlabs_request
from app.services.voice.elevenlabs_client import ElevenLabsClient, ElevenLabsMintError
from app.services.voice.gating import VoiceDenialReason, VoiceGate
from app.services.voice.session_tokens import VoiceSessionContext, VoiceSessionTokenStore
from app.services.voice.sse import stream_reply
from app.utils.http_rate_limiter import voice_rate_limit

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
    """Extract the most recent user turn — the only field of the OpenAI
    `messages` array that is allowed to drive the pipeline. System/assistant
    turns and any other payload field are ignored for that purpose."""
    for message in reversed(payload.get("messages") or []):
        if message.get("role") == "user" and message.get("content"):
            return str(message["content"])
    return ""


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
    `stream_reply` rather than streaming the pipeline itself. A pipeline
    failure and an empty reply both still end in a 200 carrying something
    speakable — a non-200 or an empty stream leaves the caller in silence,
    which ElevenLabs has no way to render as audio.
    """
    model = str(payload.get("model") or "luran-voice")

    # Tenant/session identity comes ONLY from resolving our own opaque voice
    # token server-side. Never from any EL-supplied id in the request body —
    # see spec §9 and the module docstring on VoiceSessionTokenStore.
    token = _extract_voice_token(payload)
    context: VoiceSessionContext | None = await VoiceSessionTokenStore().resolve(token)
    if context is None:
        logger.warning("Voice custom-LLM call carried an unresolvable voice token")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="unknown voice session",
        )

    user_text = _last_user_message(payload)
    if not user_text:
        return StreamingResponse(
            iter(list(stream_reply(_NO_INPUT_REPLY, model=model))),
            media_type="text/event-stream",
        )

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
    except Exception as exc:  # noqa: BLE001 — never leave a caller in silence
        logger.exception(f"Voice pipeline failed for session {context.luran_session_id}: {exc}")
        reply_text = _FALLBACK_REPLY

    # The pipeline can succeed and still return nothing speakable (e.g. an
    # empty completion). That is not an error, so it gets its own fallback
    # rather than reusing `_FALLBACK_REPLY`'s "I ran into a problem" framing.
    if not reply_text or not reply_text.strip():
        reply_text = _EMPTY_REPLY

    # Materialise the frames before responding: the reply is already
    # complete by this point, so there is nothing left to stream
    # progressively, and this keeps `db` from being touched after the
    # request scope closes. Becomes a true async generator if/when the
    # pipeline itself grows a streaming seam.
    return StreamingResponse(
        iter(list(stream_reply(reply_text, model=model))),
        media_type="text/event-stream",
    )
