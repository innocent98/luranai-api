"""Option A adapter: HTTP client for the Baileys sidecar (contract section S).

Bearer-authenticated, short timeouts, never logs the token or a pairing code.
"""

from __future__ import annotations

import re

import httpx
from loguru import logger

from app.channels.base import (
    CapacityExceeded,
    ChannelCapabilities,
    MediaBlob,
    MediaNotFound,
    MediaRejected,
    MediaTooLarge,
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
    SidecarHealth,
    SidecarUnavailable,
)
from app.core.config import settings


# The id comes from a signed webhook, but it is interpolated into a URL path: pin its shape.
_MEDIA_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_MEDIA_CHUNK = 64 * 1024


class LinkedDeviceAdapter:
    provider = "linked_device"
    # Reply-only, text + images, no buttons.
    capabilities = ChannelCapabilities(
        rich_buttons=False,
        templates_required_outside_window=False,
        max_text_len=4096,
        media_kinds=("image",),
    )

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        # `transport` lets tests mount the in-process fake sidecar.
        self._transport = transport

    def _client(self, image: bool = False) -> httpx.AsyncClient:
        if not settings.WHATSAPP_SIDECAR_URL or not settings.WHATSAPP_SIDECAR_TOKEN:
            raise SidecarUnavailable("sidecar url/token is not configured")
        return httpx.AsyncClient(
            base_url=settings.WHATSAPP_SIDECAR_URL.rstrip("/"),
            headers={"Authorization": f"Bearer {settings.WHATSAPP_SIDECAR_TOKEN}"},
            # An image send queues behind the sidecar's pause, fetch and upload: longer timeout.
            timeout=httpx.Timeout(
                (
                    settings.WHATSAPP_SIDECAR_IMAGE_TIMEOUT_S
                    if image
                    else settings.WHATSAPP_SIDECAR_TIMEOUT_S
                ),
                connect=3.0,
            ),
            transport=self._transport,
        )

    async def _request(  # type: ignore[no-untyped-def]
        self, method: str, path: str, *, image: bool = False, **kwargs
    ) -> httpx.Response:
        try:
            async with self._client(image=image) as client:
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

    async def get_health(self) -> SidecarHealth:
        """Sidecar load numbers for the capacity alert. A bad status or body is `SidecarUnavailable`."""
        resp = await self._request("GET", "/health")
        if resp.status_code != 200:
            raise SidecarUnavailable(f"sidecar_status_{resp.status_code}")
        try:
            body = resp.json()
        except ValueError:
            raise SidecarUnavailable("sidecar_health_malformed") from None
        fields = ("sessions", "max_sessions", "rss_mb", "rss_refuse_mb")
        if not isinstance(body, dict) or any(
            isinstance(body.get(f), bool) or not isinstance(body.get(f), int | float)
            for f in fields
        ):
            raise SidecarUnavailable("sidecar_health_malformed")
        return SidecarHealth(
            sessions=int(body["sessions"]),
            max_sessions=int(body["max_sessions"]),
            rss_mb=float(body["rss_mb"]),
            rss_refuse_mb=float(body["rss_refuse_mb"]),
        )

    async def delete_session(self, session_key: str) -> None:
        resp = await self._request("DELETE", f"/sessions/{session_key}")
        if resp.status_code not in (200, 404):  # idempotent: already gone is fine
            raise SidecarUnavailable(f"sidecar_status_{resp.status_code}")

    async def send(self, session_key: str, message: OutboundMessage) -> SendResult:
        if message.image_url:
            image: dict[str, str] = {"url": message.image_url}
            if message.image_caption:
                image["caption"] = message.image_caption
            payload: dict = {"to_ref": message.chat_ref, "image": image}
        else:
            payload = {"to_ref": message.chat_ref, "text": message.text}
        resp = await self._request(
            "POST",
            f"/sessions/{session_key}/send",
            image=bool(message.image_url),
            json=payload,
        )
        if resp.status_code == 200:
            return SendResult(message_id=str(resp.json()["message_id"]))
        if resp.status_code == 409:
            raise ReplyOnly()
        if resp.status_code == 429:
            raise RateCapped()
        if resp.status_code in (400, 413, 415) and message.image_url:
            # invalid_image_url / image_too_large / unsupported_media_type: retrying cannot help.
            raise MediaRejected(code=self._error_code(resp) or "media_rejected")
        if resp.status_code in (404, 503):
            # 503 not_connected and an unknown session are both hard stops (no retry).
            raise NotConnected(code=self._error_code(resp) or "not_connected")
        raise SidecarUnavailable(f"sidecar_status_{resp.status_code}")

    async def fetch_media(self, session_key: str, media_id: str, max_bytes: int) -> MediaBlob:
        """GET the stored media, streamed with a hard byte cap. Bytes are never logged."""
        if not _MEDIA_ID.fullmatch(media_id):
            raise MediaNotFound()
        try:
            async with self._client() as client:
                async with client.stream(
                    "GET", f"/sessions/{session_key}/media/{media_id}"
                ) as resp:
                    if resp.status_code == 404:
                        raise MediaNotFound()
                    if resp.status_code != 200:
                        raise SidecarUnavailable(f"sidecar_status_{resp.status_code}")
                    declared = resp.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > max_bytes:
                        raise MediaTooLarge()
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in resp.aiter_bytes(_MEDIA_CHUNK):
                        total += len(chunk)
                        if total > max_bytes:
                            raise MediaTooLarge()
                        chunks.append(chunk)
                    ctype = resp.headers.get("content-type", "application/octet-stream")
                    return MediaBlob(
                        content=b"".join(chunks),
                        content_type=ctype.split(";", 1)[0].strip().lower(),
                    )
        except httpx.HTTPError as exc:
            logger.warning("whatsapp sidecar transport error: {}", type(exc).__name__)
            raise SidecarUnavailable(type(exc).__name__) from None
