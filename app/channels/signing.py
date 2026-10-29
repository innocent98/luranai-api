"""Sidecar -> backend webhook signature (format of spikes/whatsapp/src/sign.mjs).

Header `X-Luran-Signature: t=<unix>,v1=<hex>` where v1 = HMAC-SHA256(secret, "<t>.<rawbody>").
5-minute replay window, constant-time compare, current + previous secret (rotation).

Never log any part of an expected or received signature.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import time

SIGNATURE_HEADER = "X-Luran-Signature"
REPLAY_WINDOW_S = 300
_HEADER = re.compile(r"^t=([0-9]{1,12}),v1=([0-9a-f]{64})$", re.ASCII)


def sign(secret: str, raw_body: bytes, ts: int | None = None) -> str:
    ts = int(time.time()) if ts is None else ts
    mac = hmac.new(secret.encode(), f"{ts}.".encode() + raw_body, hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"


def verify(secrets: list[str], raw_body: bytes, header: str | None, now: int | None = None) -> bool:
    match = _HEADER.fullmatch(header or "")
    if match is None:
        return False
    ts = int(match.group(1))
    now = int(time.time()) if now is None else now
    if abs(now - ts) > REPLAY_WINDOW_S:
        return False
    received = match.group(2)
    ok = False
    for secret in secrets:
        if not secret:
            continue
        expected = sign(secret, raw_body, ts).split("v1=")[1]
        # No short-circuit: always compare every configured secret.
        ok |= hmac.compare_digest(expected, received)
    return ok
