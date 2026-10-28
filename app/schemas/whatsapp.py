"""WhatsApp channel API + webhook schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

# WhatsApp text can be 65k; the chat pipeline accepts 10k.
MAX_TEXT = 10000


class ConsentOut(BaseModel):
    version: str
    text: str


class ConsentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str | None = None
    accepted: bool = False


class ConnectionCreate(BaseModel):
    """Validated in the service (not by pydantic) so errors are `{code, message}`."""

    model_config = ConfigDict(extra="forbid")

    phone_number: str = Field(..., max_length=32)
    label: str | None = Field(None, max_length=100)
    consent: ConsentIn | None = None


class ConnectionPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ai_enabled: bool | None = None
    label: str | None = Field(None, max_length=100)


class ConnectionOut(BaseModel):
    id: str
    label: str | None
    status: str
    phone_masked: str
    ai_enabled: bool
    last_connected_at: datetime | None
    last_disconnect_reason: str | None


class ConnectionCreated(BaseModel):
    id: str
    status: Literal["awaiting_pair"]
    pairing_code: str
    expires_in_s: int


class PairingCodeOut(BaseModel):
    pairing_code: str
    expires_in_s: int


# ----------------------------------------------------------------------- webhook (W)
class _EventBase(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(..., min_length=1, max_length=64)  # delivery uuid
    at: str = Field(..., max_length=64)
    session_key: str = Field(..., min_length=1, max_length=64)


class SessionStatusEvent(_EventBase):
    type: Literal["session.status"]
    status: str = Field(..., max_length=32)
    code: int | None = None


class InboundMedia(BaseModel):
    """Reference to an image held in the sidecar's media store (fetched, never inlined)."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(..., min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    mime: str = Field(..., max_length=100)
    bytes: int = Field(..., ge=0, le=100 * 1024 * 1024)
    sha256: str = Field(..., pattern=r"^[A-Fa-f0-9]{64}$")


class MessageInboundEvent(_EventBase):
    type: Literal["message.inbound"]
    message_id: str = Field(..., min_length=1, max_length=128)
    from_ref: str = Field(..., min_length=1, max_length=256)
    from_phone_digits: str | None = Field(None, max_length=20)
    push_name: str | None = Field(None, max_length=255)
    kind: Literal["text", "image", "other"]
    text: str = Field("", max_length=MAX_TEXT)  # an image's caption
    media: InboundMedia | None = None
    skipped: str | None = Field(None, max_length=64)  # e.g. media_too_large


class MessageOutboundExternalEvent(_EventBase):
    type: Literal["message.outbound_external"]
    message_id: str = Field(..., min_length=1, max_length=128)
    to_ref: str = Field(..., min_length=1, max_length=256)
    text: str = Field("", max_length=MAX_TEXT)


WebhookEvent = Annotated[
    SessionStatusEvent | MessageInboundEvent | MessageOutboundExternalEvent,
    Field(discriminator="type"),
]
