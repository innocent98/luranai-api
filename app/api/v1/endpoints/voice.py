"""Voice chat endpoints — ElevenLabs Agents integration.

Spec: docs/superpowers/specs/2026-08-15-voice-chat-backend-design.md
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from loguru import logger
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.config import settings
from app.db.models.conversation import ConversationSession
from app.schemas.voice import VoiceTokenRequest, VoiceTokenResponse
from app.services.voice.elevenlabs_client import ElevenLabsClient, ElevenLabsMintError
from app.services.voice.gating import VoiceDenialReason, VoiceGate
from app.services.voice.session_tokens import VoiceSessionTokenStore
from app.utils.http_rate_limiter import voice_rate_limit

router = APIRouter()


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
