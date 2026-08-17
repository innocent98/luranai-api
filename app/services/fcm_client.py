"""FCM HTTP v1 transport for Agent Caller escalation pushes.

Mints an OAuth2 access token from the Firebase service-account JSON using the
JWT-bearer grant (signed with python-jose, already a project dependency) and
sends one message per device token. Per-token delivery failures never raise —
they come back on the FCMResult so the caller can log and prune.

`google-auth` is deliberately not a dependency here: adding it would force a
`poetry lock` and a Docker image rebuild for ~30 lines of OAuth2 plumbing.
`_access_token()` is the seam where a future swap to `google-auth` would land
if token minting ever proves flaky in production.
"""

import json
import logging
import time
from dataclasses import dataclass

import httpx
from jose import jwt

from app.core.config import settings
from app.db.models.agent_device import DevicePlatform

logger = logging.getLogger(__name__)

_TOKEN_URL = "https://oauth2.googleapis.com/token"
_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
_SEND_URL = "https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"
_TIMEOUT = 10.0

# client_email -> (access_token, expires_at_epoch)
_token_cache: dict[str, tuple[str, float]] = {}


class FCMConfigError(RuntimeError):
    """FIREBASE_CREDENTIALS_JSON is missing or malformed."""


@dataclass
class FCMResult:
    """Outcome of a single-device send."""

    token: str
    ok: bool
    unregistered: bool = False
    error: str | None = None


def stringify_data(data: dict) -> dict[str, str]:
    """FCM `data` values must all be strings; drop Nones, stringify the rest."""
    return {k: str(v) for k, v in data.items() if v is not None}


def _credentials() -> dict:
    raw = settings.FIREBASE_CREDENTIALS_JSON
    if not raw:
        raise FCMConfigError("FIREBASE_CREDENTIALS_JSON is not configured")
    try:
        creds = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise FCMConfigError("FIREBASE_CREDENTIALS_JSON is not valid JSON") from exc
    for key in ("client_email", "private_key", "project_id"):
        if not creds.get(key):
            raise FCMConfigError(f"FIREBASE_CREDENTIALS_JSON is missing '{key}'")
    return creds


async def _access_token() -> str:
    """Return a cached or freshly minted OAuth2 access token for FCM."""
    creds = _credentials()
    now = time.time()
    cached = _token_cache.get(creds["client_email"])
    if cached is not None and cached[1] > now + 60:
        return cached[0]

    assertion = jwt.encode(
        {
            "iss": creds["client_email"],
            "scope": _SCOPE,
            "aud": _TOKEN_URL,
            "iat": int(now),
            "exp": int(now) + 3600,
        },
        creds["private_key"],
        algorithm="RS256",
    )
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(
            _TOKEN_URL,
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": assertion,
            },
        )
    resp.raise_for_status()
    payload = resp.json()
    token = payload["access_token"]
    _token_cache[creds["client_email"]] = (token, now + int(payload.get("expires_in", 3600)))
    return token


def _envelope(
    device_token: str,
    data: dict[str, str],
    platform: DevicePlatform,
    notification: dict[str, str] | None,
) -> dict:
    """Build the HTTP v1 `message` body.

    A `notification` block makes the push OS-rendered and user-visible. On Android
    that is a trap for anything the app needs to react to while backgrounded: when
    a message carries a `notification` block, the system renders it itself and
    `onBackgroundMessage` is never invoked — the app's full-screen-intent alert
    never runs, and `clear_alert` can't cancel a notification the OS (not the app)
    posted, because the plugin's `cancel(id)` only cancels notifications the app
    itself posted. So the visible escalation alert is DATA-ONLY on Android: the
    app wakes on `data`, renders its own notification on the channel it owns, and
    can therefore cancel it when the clear arrives. iOS has no background-handler
    suppression and no full-screen-intent equivalent — a data-only push displays
    nothing there — so iOS keeps the `notification` block.
    """
    visible = notification is not None
    message: dict = {"token": device_token, "data": data}
    android: dict = {"priority": "high"}
    apns_payload: dict = {"aps": {}}

    if visible:
        apns_headers = {"apns-priority": "10"}
        if platform is DevicePlatform.IOS:
            message["notification"] = notification
    else:
        apns_payload["aps"]["content-available"] = 1
        apns_headers = {"apns-priority": "5", "apns-push-type": "background"}

    message["android"] = android
    message["apns"] = {"headers": apns_headers, "payload": apns_payload}
    return message


async def send(
    device_token: str,
    data: dict[str, str],
    *,
    platform: DevicePlatform,
    notification: dict[str, str] | None = None,
) -> FCMResult:
    """Send one FCM message. Never raises on delivery failure."""
    try:
        creds = _credentials()
        access = await _access_token()
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                _SEND_URL.format(project_id=creds["project_id"]),
                headers={"Authorization": f"Bearer {access}"},
                json={"message": _envelope(device_token, data, platform, notification)},
            )
    except Exception as exc:
        logger.warning("FCM send failed before dispatch: %s", exc)
        return FCMResult(token=device_token, ok=False, error=str(exc))

    if resp.status_code == 200:
        return FCMResult(token=device_token, ok=True)

    body = resp.text or ""
    unregistered = (
        resp.status_code == 404 or "UNREGISTERED" in body or "InvalidRegistration" in body
    )
    logger.warning("FCM send rejected (%s, unregistered=%s)", resp.status_code, unregistered)
    return FCMResult(
        token=device_token,
        ok=False,
        unregistered=unregistered,
        error=f"{resp.status_code}: {body[:200]}",
    )
