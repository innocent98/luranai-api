from uuid import UUID

from pydantic import BaseModel, Field


class VoiceTokenRequest(BaseModel):
    tenant_id: UUID
    luran_session_id: UUID = Field(
        ..., description="Existing Luran conversation session to continue"
    )
    text_out: bool = Field(default=False, description="Also render assistant replies as text")


class VoiceTokenResponse(BaseModel):
    conversation_token: str
    agent_id: str
    voice_token: str
    expires_in_seconds: int


class VoiceSessionEndRequest(BaseModel):
    voice_token: str = Field(..., description="Opaque voice-session token from /session/token")


class VoiceSessionEndResponse(BaseModel):
    ok: bool = True
