"""Capture REAL responses for the conversation-counting section of the chat FE guide.

Writes docs/fe-integration-captures/conversation-counting/. Everything quoted in
docs/fe-integration-guide-conversation-counting.md is copied from these files; re-run to re-derive.

Local, throwaway infrastructure only (never staging/prod). Needs a Postgres database (DATABASE_URL)
and Redis (REDIS_URL) in the environment, plus the usual required settings (see .env.example):

    python scripts/capture_conversation_counting.py

The script starts the real FastAPI app on a local port in this process and talks to it over HTTP. The
ONLY thing replaced is the model call (reply text, retrieval, automation), so no OpenAI request is
made; every status code and body below is produced by the real routes, dependencies and database.
Visitor tokens are replaced by placeholders in the written files.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import uuid
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import httpx
import uvicorn

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
OUT = ROOT / "docs" / "fe-integration-captures" / "conversation-counting"
PORT = int(os.environ.get("CAPTURE_PORT", "8123"))
BASE = f"http://127.0.0.1:{PORT}"
OUT.mkdir(parents=True, exist_ok=True)

from sqlalchemy import select  # noqa: E402

from app.db.base import Base  # noqa: E402
from app.db.models.conversation import ConversationSession, SessionStatus  # noqa: E402
from app.db.models.tenant import Tenant, TenantAIConfig, TenantCapabilities  # noqa: E402
from app.db.session import SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.services.chat_automation_orchestrator import AutomationTurnResult  # noqa: E402
from app.utils.datetime import utcnow_naive  # noqa: E402

PLACEHOLDER = "<visitor-token-placeholder>"
_n = {"i": 0}
_secrets: list[str] = []
http = httpx.Client(timeout=30)


def _scrub(value):
    text = json.dumps(value)
    for secret in _secrets:
        text = text.replace(secret, PLACEHOLDER)
    return json.loads(text)


def record(name, method, path, *, body=None, note=""):
    _n["i"] += 1
    r = http.request(method, BASE + path, json=body)
    try:
        resp_body = r.json() if r.content else None
    except ValueError:
        resp_body = r.text
    entry = _scrub(
        {
            "scenario": name,
            "note": note,
            "request": {"method": method, "path": path, "body": body},
            "response": {"status": r.status_code, "body": resp_body},
        }
    )
    (OUT / f"{_n['i']:02d}-{name}.json").write_text(json.dumps(entry, indent=2) + "\n")
    print(f"{_n['i']:02d} {name}: {r.status_code}")
    return r


def set_session(session_id, *, status, hours_ago):
    db = SessionLocal()
    try:
        row = db.get(ConversationSession, uuid.UUID(str(session_id)))
        row.status = status
        row.last_message_at = utcnow_naive() - timedelta(hours=hours_ago)
        db.commit()
    finally:
        db.close()


def make_tenant(slug, cap):
    db = SessionLocal()
    try:
        tenant = Tenant(
            id=uuid.uuid4(),
            name=slug,
            display_name="Capture Co",
            api_key_hash=uuid.uuid4().hex,
            public_slug=slug,
            is_publicly_listed=True,
            subscription_tier="trial",
            subscription_status="trialing",
        )
        db.add(tenant)
        db.flush()
        db.add(
            TenantAIConfig(
                id=uuid.uuid4(),
                tenant_id=tenant.id,
                brand_voice={},
                escalation_keywords=[],
                max_response_tokens=250,
                grounding_requirement=True,
            )
        )
        db.add(
            TenantCapabilities(
                id=uuid.uuid4(),
                tenant_id=tenant.id,
                can_create_tickets=True,
                can_access_analytics=True,
                can_upload_knowledge=True,
                max_conversations_per_month=cap,
            )
        )
        db.commit()
        return str(tenant.id)
    finally:
        db.close()


def count_rows(tenant_id):
    db = SessionLocal()
    try:
        return len(
            db.execute(
                select(ConversationSession).where(
                    ConversationSession.tenant_id == uuid.UUID(tenant_id)
                )
            )
            .scalars()
            .all()
        )
    finally:
        db.close()


def chat(tenant_id, token, text, session_id=None):
    return {
        "message": text,
        "session_id": session_id,
        "tenant_id": tenant_id,
        "user_identifier": f"capture-{token[:6]}",
        "metadata": {"visitor_token": token, "entry_point": "widget"},
    }


def scenarios():
    slug = f"cc-{uuid.uuid4().hex[:8]}"
    tenant_id = make_tenant(slug, cap=2)
    boot = record(
        "visitor-bootstrap",
        "POST",
        "/api/v1/public/visitor/bootstrap",
        body={"workspace_slug": slug, "entry_point": "widget"},
        note="The widget bootstraps a visitor first; its token identifies the returning visitor.",
    )
    token = boot.json()["visitor_token"]
    _secrets.extend([token, boot.json()["session_token"]])
    first_file = next(OUT.glob("01-*.json"))  # written before the tokens were known: scrub it too
    first_file.write_text(json.dumps(_scrub(json.loads(first_file.read_text())), indent=2) + "\n")

    first = record(
        "first-message-creates-conversation-1",
        "POST",
        "/api/v1/chat/message",
        body=chat(tenant_id, token, "Hi, what are your hours?"),
        note="No session_id: a new conversation (counted: 1 of 2).",
    ).json()
    sid = first["session_id"]

    set_session(sid, status=SessionStatus.ABANDONED, hours_ago=5.9)
    again = record(
        "return-inside-window-same-conversation",
        "POST",
        "/api/v1/chat/message",
        body=chat(tenant_id, token, "I am back", session_id=None),
        note="5.9 h later, abandoned, no session_id sent: the same session_id comes back, reopened.",
    ).json()
    assert again["session_id"] == sid and count_rows(tenant_id) == 1

    set_session(sid, status=SessionStatus.RESOLVED, hours_ago=6.1)
    second = record(
        "return-beyond-window-new-conversation-2",
        "POST",
        "/api/v1/chat/message",
        body=chat(tenant_id, token, "Another question", session_id=sid),
        note="6.1 h later the widget still sends its stored (old) session_id: the response carries "
        "a NEW session_id. The widget must adopt it.",
    ).json()
    assert second["session_id"] != sid and count_rows(tenant_id) == 2

    follow = record(
        "client-that-keeps-the-old-id-lands-in-the-new-one",
        "POST",
        "/api/v1/chat/message",
        body=chat(tenant_id, token, "Still the old id", session_id=sid),
        note="A client that did not adopt the new id keeps being routed to it: no third conversation.",
    ).json()
    assert follow["session_id"] == second["session_id"] and count_rows(tenant_id) == 2

    over = record(
        "cap-reached-new-visitor-402",
        "POST",
        "/api/v1/chat/message",
        body={
            "message": "hello",
            "tenant_id": tenant_id,
            "user_identifier": "someone-new",
        },
        note="2 of 2 used: a brand-new visitor is refused.",
    )
    assert over.status_code == 402

    record(
        "cap-reached-create-session-402",
        "POST",
        "/api/v1/chat/sessions",
        body={"tenant_id": tenant_id, "user_identifier": "someone-new"},
        note="POST /chat/sessions was ungated before; it now returns the same 402 body.",
    )

    set_session(second["session_id"], status=SessionStatus.ABANDONED, hours_ago=1)
    cont = record(
        "cap-reached-returning-inside-window-still-served",
        "POST",
        "/api/v1/chat/message",
        body=chat(tenant_id, token, "Are you there?", session_id=None),
        note="At the cap, a visitor continuing inside the window is NOT blocked (not a new conversation).",
    )
    assert cont.status_code == 200

    set_session(second["session_id"], status=SessionStatus.ABANDONED, hours_ago=7)
    stale = record(
        "cap-reached-returning-beyond-window-402",
        "POST",
        "/api/v1/chat/message",
        body=chat(tenant_id, token, "Back after a long time", session_id=second["session_id"]),
        note="Beyond the window this would be a new conversation, so at the cap it is the same 402.",
    )
    assert stale.status_code == 402


def main():
    Base.metadata.create_all(bind=engine)

    async def _gen(
        self, prompt, max_tokens=800, model_name=None, system_message=None, image_urls=None
    ):
        return "We open 9am to 5pm, Monday to Friday.", 30, False

    async def _retr(self, *, tenant_id, query, max_chunks, score_threshold):
        return [], []

    async def _auto(self, **kwargs):
        return AutomationTurnResult(handled=False)

    with (
        patch("app.services.rag_service.RAGService.generate_ai_response", new=_gen),
        patch("app.services.rag_service.RAGService.retrieve_relevant_chunks", new=_retr),
        patch(
            "app.services.chat_automation_orchestrator.ChatAutomationOrchestrator.handle_turn",
            new=_auto,
        ),
    ):
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning")
        )
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.1)
        try:
            scenarios()
        finally:
            server.should_exit = True
            thread.join(timeout=10)


if __name__ == "__main__":
    main()
