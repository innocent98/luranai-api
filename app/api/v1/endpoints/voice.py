"""Voice chat endpoints — ElevenLabs Agents integration.

Spec: docs/superpowers/specs/2026-08-15-voice-chat-backend-design.md
"""

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

router = APIRouter()


@router.post(
    "/session/token",
    response_model=VoiceTokenResponse,
    status_code=status.HTTP_200_OK,
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

        try:
            conversation_token = await ElevenLabsClient().mint_conversation_token(
                agent_id=settings.ELEVENLABS_AGENT_ID,
                dynamic_variables={"voice_token": voice_token},
            )
        except ElevenLabsMintError as exc:
            # Don't leave an orphaned voice-token binding or a leaked
            # concurrency slot behind for a mint that never happened.
            await gate.release_slot(payload.tenant_id, voice_token)
            await store.revoke(voice_token)
            logger.error(f"Voice token mint failed: {exc}")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"reason": "voice_provider_unavailable"},
            ) from exc
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
