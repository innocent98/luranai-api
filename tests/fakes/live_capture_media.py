"""Record real exchanges for the media + rating part of the WhatsApp FE guide.

Same rig as `live_capture.py` (migrated scratch Postgres, Redis, `tests.fakes.live_stub api` +
`worker`, the fake sidecar) with the LLM, moderation and Cloudinary stubbed. NOT part of the
test suite. Output: docs/fe-integration-captures/whatsapp-media-rating/.
"""

from __future__ import annotations

import io
import json
import os
import sys
import uuid

os.environ.setdefault("CAPTURE_OUT", "docs/fe-integration-captures/whatsapp-media-rating")

from PIL import Image  # noqa: E402

from tests.fakes import live_capture as lc  # noqa: E402
from tests.fakes.live_capture import (  # noqa: E402
    API,
    FAKE,
    OUT,
    client,
    db_value,
    emit,
    login,
    record,
    seed,
    sent_redacted,
    wait_for,
)

CUSTOMER = "198765432100@lid"


def png(size=(24, 24), colour="teal") -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, colour).save(out, format="PNG")
    return out.getvalue()


def stage(key: str, content: bytes, mime="image/png") -> dict:
    r = client.post(f"{FAKE}/_fake/media/{key}", content=content, headers={"content-type": mime})
    r.raise_for_status()
    return r.json()


def inbound(key, message_id, **fields):
    return emit(
        key,
        {
            "type": "message.inbound",
            "message_id": message_id,
            "from_ref": CUSTOMER,
            "from_phone_digits": None,
            "push_name": "Ada",
            "kind": "text",
            "text": "",
            **fields,
        },
    )


def sidecar_sent():
    return client.get(f"{FAKE}/_fake/sent").json()


def save(name: str, payload) -> None:
    (OUT / name).write_text(json.dumps(payload, indent=2) + "\n")


def main() -> None:
    tenants = seed()
    platform = login(
        os.environ["FIRST_SUPERUSER_EMAIL"],
        os.environ["FIRST_SUPERUSER_PASSWORD"],
        path="/api/v1/platform/auth/login",
    )
    owner = login("owner-alpha@example.com", "Capture-Pass-123!")
    record(
        "platform-grant",
        "PUT",
        f"/api/v1/platform/tenants/{tenants['alpha']}/capabilities",
        token=platform,
        json_body={"can_use_whatsapp": True, "max_whatsapp_numbers": 1},
    )
    consent = client.get(
        f"{API}/api/v1/whatsapp/consent", headers={"Authorization": f"Bearer {owner}"}
    ).json()
    created = client.post(
        f"{API}/api/v1/whatsapp/connections",
        headers={"Authorization": f"Bearer {owner}"},
        json={
            "phone_number": "2348000000001",
            "label": "Main line",
            "consent": {"version": consent["version"], "accepted": True},
        },
    ).json()
    cid = created["id"]
    key = db_value("SELECT session_key FROM whatsapp_connections WHERE id = :i", i=cid)
    client.post(f"{FAKE}/_fake/status/{key}", json={"status": "connecting"})
    client.post(f"{FAKE}/_fake/status/{key}", json={"status": "connected"})
    wait_for(
        lambda: client.get(
            f"{API}/api/v1/whatsapp/connections/{cid}", headers={"Authorization": f"Bearer {owner}"}
        )
        .json()
        .get("status")
        == "connected",
        "connected",
    )
    client.put(
        f"{API}/api/v1/tenant/ai-config",
        headers={"Authorization": f"Bearer {owner}"},
        json={"first_responder": "ai", "grounding_requirement": False},
    )

    # --- 1. customer image + caption -> AI sees it -----------------------------------------
    media = stage(key, png())
    save(
        "00-sidecar-media-staged.json",
        {
            "note": "media object a message.inbound image event carries",
            "media": {**media, "id": "<media-id-placeholder>"},
        },
    )
    inbound(key, "IMG0001", kind="image", text="Is this the right model?", media=media)
    wait_for(lambda: len(sidecar_sent()) >= 1, "AI reply to the image")
    save("01-sidecar-sent-after-image.json", {"fake_sidecar_received": sent_redacted()})
    sid = wait_for(lambda: db_value("SELECT session_id FROM whatsapp_chats LIMIT 1"), "session")
    record(
        "staff-session-detail-after-image",
        "GET",
        f"/api/v1/chat/sessions/{sid}",
        token=owner,
        note="Customer image message with attachments; the AI reply says how many images it saw (LLM stubbed).",
    )

    # --- 2. moderation block: nothing stored, one fixed sentence -----------------------------
    before = len(sidecar_sent())
    flagged = stage(key, png((7, 7), "red"))
    inbound(key, "IMG0002", kind="image", text="", media=flagged)
    wait_for(lambda: len(sidecar_sent()) > before, "block sentence")
    save(
        "03-sidecar-sent-after-blocked-image.json",
        {"fake_sidecar_received": sent_redacted()[before:]},
    )

    # --- 3. integrity: wrong sha256 -> polite miss, nothing stored ---------------------------
    before = len(sidecar_sent())
    bad = {**stage(key, png((30, 30), "navy")), "sha256": "0" * 64}
    inbound(key, "IMG0003", kind="image", text="", media=bad)
    wait_for(lambda: len(sidecar_sent()) > before, "unreadable sentence")
    save(
        "04-sidecar-sent-after-corrupt-image.json",
        {"fake_sidecar_received": sent_redacted()[before:]},
    )

    # --- 4. AI off: image stored for agents, no reply ----------------------------------------
    client.patch(
        f"{API}/api/v1/whatsapp/connections/{cid}",
        headers={"Authorization": f"Bearer {owner}"},
        json={"ai_enabled": False},
    )
    before = len(sidecar_sent())
    inbound(
        key,
        "IMG0004",
        kind="image",
        text="here is the receipt",
        media=stage(key, png((40, 40), "gold")),
    )
    wait_for(
        lambda: db_value("SELECT count(*) FROM message_attachments WHERE direction = 'INBOUND'")
        == 2,
        "second image stored",
    )
    save(
        "05-sidecar-sent-while-ai-off.json",
        {
            "note": "sends after the AI-off image (expected none)",
            "fake_sidecar_received": sent_redacted()[before:],
        },
    )
    record("staff-session-detail-ai-off", "GET", f"/api/v1/chat/sessions/{sid}", token=owner)

    # --- 5. the fallback fix: a voice note while a human owns the chat ----------------------
    db_value("SELECT 1")
    from sqlalchemy import text as sql
    from app.db.session import SessionLocal

    with SessionLocal() as db:
        db.execute(
            sql("UPDATE conversation_sessions SET status = 'ESCALATED' WHERE id = :i"), {"i": sid}
        )
        db.commit()
    before = len(sidecar_sent())
    inbound(key, "OTH0001", kind="other", text="")

    def _placeholder_stored() -> bool:  # message content is encrypted at rest: read it via the API
        detail = client.get(
            f"{API}/api/v1/chat/sessions/{sid}", headers={"Authorization": f"Bearer {owner}"}
        ).json()
        return any(
            m["content"].startswith("[The customer sent a voice note") for m in detail["messages"]
        )

    wait_for(_placeholder_stored, "placeholder stored")
    save(
        "06-sidecar-sent-after-voice-note-while-human-owns.json",
        {"note": "expected none", "fake_sidecar_received": sent_redacted()[before:]},
    )
    record(
        "staff-session-detail-after-voice-note", "GET", f"/api/v1/chat/sessions/{sid}", token=owner
    )
    with SessionLocal() as db:  # back to AI-answerable for the rest of the run
        db.execute(
            sql("UPDATE conversation_sessions SET status = 'ACTIVE' WHERE id = :i"), {"i": sid}
        )
        db.commit()
    client.patch(
        f"{API}/api/v1/whatsapp/connections/{cid}",
        headers={"Authorization": f"Bearer {owner}"},
        json={"ai_enabled": True},
    )
    inbound(key, "OTH0002", kind="other", text="")
    wait_for(lambda: len(sidecar_sent()) > before, "fixed reply when nobody owns it")
    save(
        "07-sidecar-sent-after-voice-note-nobody-owns.json",
        {"fake_sidecar_received": sent_redacted()[before:]},
    )

    # --- 6. agent image -> WhatsApp ------------------------------------------------------------
    before = len(sidecar_sent())
    up = client.post(
        f"{API}/api/v1/chat/sessions/{sid}/upload",
        headers={"Authorization": f"Bearer {owner}"},
        files={"file": ("diagram.png", png((50, 50), "purple"), "image/png")},
    )
    lc._counter += 1
    save(
        f"{lc._counter:02d}-agent-upload.json",
        {
            "request": {
                "method": "POST",
                "path": f"/api/v1/chat/sessions/{sid}/upload",
                "body": "multipart file=diagram.png",
            },
            "response": {"status": up.status_code, "body": up.json()},
        },
    )
    att_id = up.json()["attachment_id"]
    record(
        "agent-message-with-image",
        "POST",
        f"/api/v1/chat/sessions/{sid}/agent-message",
        token=owner,
        json_body={"message": "Here is the setup diagram", "attachment_ids": [att_id]},
    )
    wait_for(lambda: len(sidecar_sent()) > before, "image delivered to the sidecar")
    save(
        "08-sidecar-sent-after-agent-image.json",
        {"fake_sidecar_received": sent_redacted()[before:]},
    )

    # --- 7. rating request forwarded + answered ------------------------------------------------
    with SessionLocal() as db:
        agent_id = db.execute(
            sql("SELECT id FROM users WHERE tenant_id = :t LIMIT 1"), {"t": tenants["alpha"]}
        ).scalar_one()
        tid = str(uuid.uuid4())
        db.execute(
            sql(
                "INSERT INTO tickets (id, tenant_id, session_id, ticket_number, summary, priority, status, assigned_to, created_at, updated_at) "
                "SELECT :t, tenant_id, id, 'TKT-CAPTURE-1', 'Capture', 'MEDIUM', 'IN_PROGRESS', :a, now(), now() "
                "FROM conversation_sessions WHERE id = :s"
            ),
            {"t": tid, "a": str(agent_id), "s": str(sid)},
        )
        db.commit()
    before = len(sidecar_sent())
    record("rating-request", "POST", f"/api/v1/tickets/{tid}/rating-requests", token=owner)
    wait_for(lambda: len(sidecar_sent()) > before, "rating prompt delivered")
    save("10-sidecar-sent-rating-prompt.json", {"fake_sidecar_received": sent_redacted()[before:]})
    wait_for(
        lambda: db_value("SELECT rating_request_id FROM whatsapp_chats LIMIT 1"),
        "prompt remembered",
    )

    before = len(sidecar_sent())
    inbound(key, "RATE0001", text="5")
    wait_for(lambda: len(sidecar_sent()) > before, "thanks sent")
    save("11-sidecar-sent-rating-ack.json", {"fake_sidecar_received": sent_redacted()[before:]})
    record("ratings-list-after-whatsapp-reply", "GET", "/api/v1/reviews/session", token=owner)

    before = len(sidecar_sent())
    inbound(key, "RATE0002", text="1")
    wait_for(lambda: len(sidecar_sent()) > before, "replay treated as ordinary text")
    save(
        "13-sidecar-sent-after-replayed-rating.json",
        {
            "note": "the second answer went through the normal AI flow",
            "fake_sidecar_received": sent_redacted()[before:],
        },
    )
    record("ratings-list-after-replay", "GET", "/api/v1/reviews/session", token=owner)

    # --- 8. an agent takes over: the customer gets the join line ---------------------------
    with SessionLocal() as db:  # the scenario ticket is no longer needed; no ticket = plain takeover
        db.execute(sql("DELETE FROM tickets"))
        db.commit()
    before = len(sidecar_sent())
    record("agent-takeover", "POST", f"/api/v1/chat/sessions/{sid}/takeover", token=owner)
    wait_for(lambda: len(sidecar_sent()) > before, "join line delivered")
    save("21-sidecar-sent-join-line.json", {"fake_sidecar_received": sent_redacted()[before:]})
    print("captures written to", OUT)


if __name__ == "__main__":
    sys.exit(main())
