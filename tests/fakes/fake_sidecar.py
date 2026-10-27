"""In-process fake of the WhatsApp sidecar (contract section S) for tests and live captures.

NOT the real sidecar: no Baileys, no WhatsApp, no RAM. It implements the HTTP contract with
bearer auth, a session map, a capacity cap, and test hooks under `/_fake/*`:

* `FakeSidecar.app` is a FastAPI app. In tests mount it with `httpx.ASGITransport`.
* `python -m tests.fakes.fake_sidecar --port 8099 --backend http://127.0.0.1:8000
  --secret <webhook secret> --token <bearer>` runs it as a real server; `/_fake/emit` then
  POSTs *signed* webhooks to the backend, which is how the FE guide captures were produced.

Pairing codes are obviously fake (`FAKE` + 4 digits).
"""

from __future__ import annotations

import argparse
import json
import secrets
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from app.channels import signing


class FakeSidecar:
    def __init__(
        self,
        token: str = "fake-sidecar-token",
        max_sessions: int = 3,
        backend_url: str | None = None,
        webhook_secret: str | None = None,
    ) -> None:
        self.token = token
        self.max_sessions = max_sessions
        self.backend_url = backend_url
        self.webhook_secret = webhook_secret
        self.sessions: dict[str, dict[str, Any]] = {}
        self.sent: list[dict[str, Any]] = []
        self.calls: list[tuple[str, str]] = []
        self.send_error: tuple[int, str] | None = None  # force /send to fail
        self.app = self._build()

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _code() -> str:
        return "FAKE" + "".join(secrets.choice("0123456789") for _ in range(4))

    def _auth(self, authorization: str | None) -> None:
        if authorization != f"Bearer {self.token}":
            raise HTTPException(status_code=401, detail="unauthorized")

    def set_status(self, key: str, status: str, code: int | None = None) -> None:
        sess = self.sessions[key]
        sess["status"] = status
        sess["since_ms"] = int(time.time() * 1000)
        if status in ("logged_out", "reconnecting", "failed") and code is not None:
            sess["last_disconnect"] = {"code": code, "at": datetime.now(UTC).isoformat()}

    # ------------------------------------------------------------------ app
    def _build(self) -> FastAPI:
        app = FastAPI(title="fake whatsapp sidecar")

        @app.post("/sessions")
        async def create(request: Request, authorization: str | None = Header(None)):
            self._auth(authorization)
            body = await request.json()
            key = body["session_key"]
            self.calls.append(("POST", "/sessions"))
            if key in self.sessions:
                return JSONResponse({"error": "session_exists"}, status_code=409)
            live = [s for s in self.sessions.values() if s["status"] != "logged_out"]
            if len(live) >= self.max_sessions:
                return JSONResponse(
                    {"error": "capacity_exceeded", "max_sessions": self.max_sessions},
                    status_code=503,
                )
            self.sessions[key] = {
                "status": "awaiting_pair",
                "since_ms": int(time.time() * 1000),
                "last_disconnect": None,
                "phone_digits": body["phone_digits"],
                "replied_to": set(),
            }
            return JSONResponse(
                {"status": "awaiting_pair", "pairing_code": self._code(), "expires_in_s": 180},
                status_code=201,
            )

        @app.post("/sessions/{key}/pairing-code")
        async def pairing(key: str, authorization: str | None = Header(None)):
            self._auth(authorization)
            self.calls.append(("POST", "/sessions/{key}/pairing-code"))
            sess = self.sessions.get(key)
            if sess is None:
                return JSONResponse({"error": "not_found"}, status_code=404)
            if sess["status"] not in ("awaiting_pair", "logged_out"):
                return JSONResponse({"error": "not_pairable"}, status_code=409)
            sess["status"] = "awaiting_pair"
            return {"pairing_code": self._code(), "expires_in_s": 180}

        @app.get("/sessions/{key}")
        async def status(key: str, authorization: str | None = Header(None)):
            self._auth(authorization)
            sess = self.sessions.get(key)
            if sess is None:
                return JSONResponse({"error": "not_found"}, status_code=404)
            return {
                "status": sess["status"],
                "since_ms": sess["since_ms"],
                "last_disconnect": sess["last_disconnect"],
            }

        @app.delete("/sessions/{key}")
        async def delete(key: str, authorization: str | None = Header(None)):
            self._auth(authorization)
            self.calls.append(("DELETE", "/sessions/{key}"))
            self.sessions.pop(key, None)  # idempotent
            return {"status": "logged_out"}

        @app.post("/sessions/{key}/send")
        async def send(key: str, request: Request, authorization: str | None = Header(None)):
            self._auth(authorization)
            body = await request.json()
            self.calls.append(("POST", "/sessions/{key}/send"))
            sess = self.sessions.get(key)
            if sess is None or sess["status"] != "connected":
                return JSONResponse({"error": "not_connected"}, status_code=503)
            if self.send_error is not None:
                code, err = self.send_error
                return JSONResponse({"error": err}, status_code=code)
            message_id = "FAKEMSG" + uuid.uuid4().hex[:12].upper()
            self.sent.append({"key": key, "to_ref": body["to_ref"], "text": body["text"]})
            return {"message_id": message_id}

        @app.get("/health")
        async def health(authorization: str | None = Header(None)):
            self._auth(authorization)
            return {
                "sessions": len(self.sessions),
                "max_sessions": self.max_sessions,
                "rss_mb": 120,
                "rss_refuse_mb": 400,
            }

        # ---- test / capture hooks (not part of the contract) ----
        @app.post("/_fake/status/{key}")
        async def fake_status(key: str, request: Request):
            body = await request.json()
            self.set_status(key, body["status"], body.get("code"))
            if self.backend_url:
                await self.emit(key, "session.status", status=body["status"], code=body.get("code"))
            return {"ok": True}

        @app.post("/_fake/emit/{key}")
        async def fake_emit(key: str, request: Request):
            body = await request.json()
            resp = await self.emit(key, body.pop("type"), **body)
            return {"backend_status": resp.status_code, "backend_body": resp.json()}

        @app.get("/_fake/sent")
        async def fake_sent():
            return self.sent

        return app

    # ------------------------------------------------------------------ webhooks out
    def build_event(self, key: str, etype: str, **fields: Any) -> dict[str, Any]:
        return {
            "id": str(uuid.uuid4()),
            "at": datetime.now(UTC).isoformat(),
            "session_key": key,
            "type": etype,
            **fields,
        }

    async def emit(self, key: str, etype: str, **fields: Any) -> httpx.Response:
        assert self.backend_url and self.webhook_secret, "backend_url + webhook_secret required"
        raw = json.dumps(self.build_event(key, etype, **fields)).encode()
        async with httpx.AsyncClient(timeout=10) as client:
            return await client.post(
                f"{self.backend_url.rstrip('/')}/api/v1/webhooks/whatsapp/linked",
                content=raw,
                headers={
                    "Content-Type": "application/json",
                    signing.SIGNATURE_HEADER: signing.sign(self.webhook_secret, raw),
                },
            )


def main() -> None:  # pragma: no cover - manual / capture use
    import uvicorn

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--token", required=True)
    parser.add_argument("--backend", default="http://127.0.0.1:8000")
    parser.add_argument("--secret", required=True)
    parser.add_argument("--max-sessions", type=int, default=3)
    args = parser.parse_args()
    fake = FakeSidecar(
        token=args.token,
        max_sessions=args.max_sessions,
        backend_url=args.backend,
        webhook_secret=args.secret,
    )
    uvicorn.run(fake.app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":  # pragma: no cover
    main()
