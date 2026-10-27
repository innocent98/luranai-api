"""Option A adapter: HTTP client for the Baileys sidecar (contract section S).

Bearer-authenticated, short timeouts, never logs the token or a pairing code.
"""

from __future__ import annotations

import httpx
from loguru import logger

from app.channels.base import (
    CapacityExceeded,
    ChannelCapabilities,
    NotConnected,
    NotPairable,
    OutboundMessage,
    PairingResult,
    RateCapped,
    RemoteStatus,
    ReplyOnly,
    SendResult,
    SessionExists,
    SessionNotFound,
    SidecarUnavailable,
)
from app.core.config import settings


class LinkedDeviceAdapter:
    provider = "linked_device"
    # Reply-only, text only, no buttons (the demo slice).
    capabilities = ChannelCapabilities(
        rich_buttons=False,
        templates_required_outside_window=False,
        max_text_len=4096,
        media_kinds=(),
    )

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        # `transport` lets tests mount the in-process fake sidecar.
        self._transport = transport

    def _client(self) -> httpx.AsyncClient:
        if not settings.WHATSAPP_SIDECAR_URL:
            raise SidecarUnavailable("WHATSAPP_SIDECAR_URL is not configured")
        return httpx.AsyncClient(
            base_url=settings.WHATSAPP_SIDECAR_URL.rstrip("/"),
            headers={"Authorization": f"Bearer {settings.WHATSAPP_SIDECAR_TOKEN}"},
            timeout=httpx.Timeout(settings.WHATSAPP_SIDECAR_TIMEOUT_S, connect=3.0),
            transport=self._transport,
        )

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:  # type: ignore[no-untyped-def]
        try:
            async with self._client() as client:
                return await client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            # Log only the exception class: the message can embed the URL/headers.
            logger.warning("whatsapp sidecar transport error: {}", type(exc).__name__)
            raise SidecarUnavailable(type(exc).__name__) from None

    @staticmethod
    def _error_code(resp: httpx.Response) -> str:
        try:
            body = resp.json()
        except ValueError:
            return ""
        return str(body.get("error", "")) if isinstance(body, dict) else ""

    @staticmethod
    def _pairing(resp: httpx.Response) -> PairingResult:
        body = resp.json()
        return PairingResult(
            pairing_code=str(body["pairing_code"]),
            expires_in_s=int(body.get("expires_in_s", 180)),
            status=str(body.get("status", "awaiting_pair")),
        )

    async def create_session(self, session_key: str, phone_digits: str) -> PairingResult:
        resp = await self._request(
            "POST", "/sessions", json={"session_key": session_key, "phone_digits": phone_digits}
        )
        if resp.status_code == 201:
            return self._pairing(resp)
        if resp.status_code == 409:
            raise SessionExists()
        if resp.status_code == 503 and self._error_code(resp) == "capacity_exceeded":
            raise CapacityExceeded()
        raise SidecarUnavailable(f"sidecar_status_{resp.status_code}")

    async def new_pairing_code(self, session_key: str) -> PairingResult:
        resp = await self._request("POST", f"/sessions/{session_key}/pairing-code")
        if resp.status_code == 200:
            return self._pairing(resp)
        if resp.status_code == 404:
            raise SessionNotFound()
        if resp.status_code == 409:
            raise NotPairable()
        if resp.status_code == 503 and self._error_code(resp) == "capacity_exceeded":
            raise CapacityExceeded()
        raise SidecarUnavailable(f"sidecar_status_{resp.status_code}")

    async def get_status(self, session_key: str) -> RemoteStatus:
        resp = await self._request("GET", f"/sessions/{session_key}")
        if resp.status_code == 404:
            raise SessionNotFound()
        if resp.status_code != 200:
            raise SidecarUnavailable(f"sidecar_status_{resp.status_code}")
        body = resp.json()
        last = body.get("last_disconnect") or {}
        return RemoteStatus(
            status=str(body["status"]),
            since_ms=body.get("since_ms"),
            last_disconnect_code=last.get("code"),
        )

    async def delete_session(self, session_key: str) -> None:
        resp = await self._request("DELETE", f"/sessions/{session_key}")
        if resp.status_code not in (200, 404):  # idempotent: already gone is fine
            raise SidecarUnavailable(f"sidecar_status_{resp.status_code}")

    async def send(self, session_key: str, message: OutboundMessage) -> SendResult:
        resp = await self._request(
            "POST",
            f"/sessions/{session_key}/send",
            json={"to_ref": message.chat_ref, "text": message.text},
        )
        if resp.status_code == 200:
            return SendResult(message_id=str(resp.json()["message_id"]))
        if resp.status_code == 409:
            raise ReplyOnly()
        if resp.status_code == 429:
            raise RateCapped()
        if resp.status_code in (404, 503):
            # 503 not_connected and an unknown session are both hard stops (no retry).
            raise NotConnected(code=self._error_code(resp) or "not_connected")
        raise SidecarUnavailable(f"sidecar_status_{resp.status_code}")
