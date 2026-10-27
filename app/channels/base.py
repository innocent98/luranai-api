"""Channel abstraction: provider-neutral types and the adapter protocol.

One `ChannelAdapter` per provider. Today only `linked_device` (Baileys sidecar) is real;
`cloud_api` (Meta) is a stub slot so the registry and callers need no change later.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class InboundMessage:
    provider_message_id: str
    chat_ref: str  # opaque, reply target (may be a LID, NOT a phone number)
    kind: str  # "text" | "other"
    text: str
    sender_phone_digits: str | None = None
    push_name: str | None = None
    received_at: datetime | None = None


@dataclass(frozen=True)
class OutboundMessage:
    chat_ref: str
    text: str


@dataclass(frozen=True)
class SendResult:
    message_id: str


@dataclass(frozen=True)
class PairingResult:
    """Pairing code is returned to the caller ONCE; callers must not store or log it."""

    pairing_code: str = field(repr=False)
    expires_in_s: int = 180
    status: str = "awaiting_pair"


@dataclass(frozen=True)
class RemoteStatus:
    status: str
    since_ms: int | None = None
    last_disconnect_code: int | None = None


@dataclass(frozen=True)
class ChannelCapabilities:
    rich_buttons: bool = False
    templates_required_outside_window: bool = False
    max_text_len: int = 4096
    media_kinds: tuple[str, ...] = ()


class ChannelError(Exception):
    """Base error. `code` is the machine-readable reason (never contains secrets)."""

    code = "channel_error"

    def __init__(self, message: str = "", *, code: str | None = None) -> None:
        super().__init__(message or (code or self.code))
        if code:
            self.code = code


class CapacityExceeded(ChannelError):
    code = "capacity_exceeded"


class SessionExists(ChannelError):
    code = "session_exists"


class SessionNotFound(ChannelError):
    code = "session_not_found"


class NotPairable(ChannelError):
    code = "not_pairable"


class NotConnected(ChannelError):
    """Sidecar says the session is not connected: a hard stop, never retried."""

    code = "not_connected"


class ReplyOnly(ChannelError):
    """Linked-device numbers may only reply to chats that messaged us first."""

    code = "reply_only"


class RateCapped(ChannelError):
    code = "rate_cap"


class SidecarUnavailable(ChannelError):
    """Transport failure / 5xx / timeout: transient, retried with backoff."""

    code = "sidecar_unavailable"


class ChannelAdapter(Protocol):
    provider: str
    capabilities: ChannelCapabilities

    async def create_session(self, session_key: str, phone_digits: str) -> PairingResult: ...

    async def new_pairing_code(self, session_key: str) -> PairingResult: ...

    async def get_status(self, session_key: str) -> RemoteStatus: ...

    async def delete_session(self, session_key: str) -> None: ...

    async def send(self, session_key: str, message: OutboundMessage) -> SendResult: ...
