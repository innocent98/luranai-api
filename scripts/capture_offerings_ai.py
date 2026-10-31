"""Capture REAL responses of the Offerings AI path (S2) against a RUNNING app.

Writes docs/fe-integration-captures/offerings-ai/. Everything quoted in
docs/fe-integration-guide-offerings-part2.md is copied from these files; re-run to re-derive.

Local, throwaway infrastructure only (never staging/prod): Postgres migrated to head, Redis, the
fake WhatsApp sidecar, and the API + arq worker started with `tests/fakes/live_stub_offerings.py`
(the MODEL is a stand-in that reads the injected catalog block; lookup, block, price guard, cart,
quote, notifications, webhook and worker are real; Cloudinary and moderation are stubs):

    python -m tests.fakes.fake_sidecar --port 8599 --backend http://127.0.0.1:8592 \
        --secret $WHATSAPP_WEBHOOK_SECRET --token $WHATSAPP_SIDECAR_TOKEN
    python -m tests.fakes.live_stub_offerings api --port 8592
    python -m tests.fakes.live_stub_offerings worker
    CAPTURE_API=http://127.0.0.1:8592 CAPTURE_FAKE=http://127.0.0.1:8599 \
        python scripts/capture_offerings_ai.py

Authorization headers are replaced by placeholders and throwaway passwords are never written. All
business data is fake (`example.com`, invented items and phone numbers).
"""

import io
import json
import os
import sys
import time
import uuid
from pathlib import Path

import httpx
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
OUT = ROOT / "docs" / "fe-integration-captures" / "offerings-ai"
API = os.environ.get("CAPTURE_API", "http://127.0.0.1:8592")
FAKE = os.environ.get("CAPTURE_FAKE", "http://127.0.0.1:8599")
PASSWORD = "Throwaway-Passw0rd!-local"  # local DB only; never written to a capture

OUT.mkdir(parents=True, exist_ok=True)
for old in OUT.glob("*.json"):
    old.unlink()
http = httpx.Client(timeout=60)
_n = {"i": 0}


def _write(name, payload):
    _n["i"] += 1
    (OUT / f"{_n['i']:02d}-{name}.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    )
    print(f"{_n['i']:02d} {name}")


def record(name, method, path, *, token=None, body=None, params=None, note=""):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    r = http.request(method, API + path, headers=headers, json=body, params=params)
    try:
        resp_body = r.json() if r.content else None
    except ValueError:
        resp_body = r.text
    request = {
        "method": method,
        "path": path + (("?" + str(httpx.QueryParams(params))) if params else ""),
        "headers": {"Authorization": "Bearer <jwt-placeholder>"} if token else {},
    }
    if body is not None:
        request["body"] = body
    _write(
        name,
        {
            "scenario": name,
            "note": note,
            "request": request,
            "response": {"status": r.status_code, "body": resp_body},
        },
    )
    return r


def save(name, payload, note=""):
    _write(name, {"scenario": name, "note": note, **payload})


def wait_for(predicate, what, timeout=30.0):
    end = time.time() + timeout
    while time.time() < end:
        value = predicate()
        if value:
            return value
        time.sleep(0.4)
    raise SystemExit(f"timed out waiting for {what}")


# --- database helpers (throwaway DB) ----------------------------------------------------------


def db_rows(sql, **params):
    from sqlalchemy import text

    from app.db.session import SessionLocal

    s = SessionLocal()
    try:
        return [dict(r._mapping) for r in s.execute(text(sql), params)]
    finally:
        s.close()


def db_value(sql, **params):
    rows = db_rows(sql, **params)
    return next(iter(rows[0].values())) if rows else None


def make_tenant(tag, tier="professional"):
    from sqlalchemy import select

    from app.db.models.tenant import Tenant
    from app.db.models.user import UserRole
    from app.db.session import SessionLocal
    from app.schemas.tenant import TenantCreate
    from app.schemas.user import UserCreate
    from app.services.auth_service import AuthService
    from app.services.subscription_service import SubscriptionService
    from app.services.tenant_service import TenantService

    s = SessionLocal()
    try:
        tenant = TenantService(s).create_tenant(
            TenantCreate(
                name=f"ofai_{tag}_{uuid.uuid4().hex[:6]}", display_name=f"Acme {tag} Shoes"
            )
        )
        email = f"owner-{tag}-{uuid.uuid4().hex[:6]}@example.com"
        AuthService(s).create_user(
            UserCreate(
                email=email,
                full_name=f"{tag} Owner",
                role=UserRole.TENANT_ADMIN,
                password=PASSWORD,
                tenant_id=tenant.id,
            )
        )
        tenant_id = tenant.id
        s.commit()
        row = s.execute(select(Tenant).where(Tenant.id == tenant_id)).scalar_one()
        SubscriptionService(s)._update_capabilities_for_tier(row.id, tier)
        row.subscription_tier = tier
        row.subscription_status = "active"
        s.commit()
    finally:
        s.close()
    return str(tenant_id), email


def login(email):
    r = http.post(API + "/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    r.raise_for_status()
    return r.json()["access_token"]


def jpeg(color):
    buf = io.BytesIO()
    Image.new("RGB", (160, 120), color).save(buf, format="JPEG")
    return buf.getvalue()


def authed(method, path, token, **kw):
    r = http.request(method, API + path, headers={"Authorization": f"Bearer {token}"}, **kw)
    r.raise_for_status()
    return r


CATALOG = [
    dict(
        kind="product",
        name="Canvas sneaker black",
        price_mode="fixed",
        price=12000,
        availability="in_stock",
        category="Sneakers",
        keywords="trainers",
    ),
    dict(
        kind="product",
        name="Canvas sneaker white",
        price_mode="fixed",
        price=13500,
        availability="in_stock",
        category="Sneakers",
    ),
    dict(
        kind="product",
        name="Leather bag",
        price_mode="fixed",
        price=45000,
        availability="in_stock",
        category="Bags",
        negotiable=True,
    ),
    dict(
        kind="service",
        name="Phone repair",
        price_mode="from",
        price=5000,
        price_note="per phone",
        availability="in_stock",
        category="Services",
    ),
    dict(
        kind="product",
        name="Beach sandals",
        price_mode="fixed",
        price=8000,
        availability="out_of_stock",
        category="Sandals",
    ),
]


def seed_catalog(token):
    ids = []
    for body in CATALOG:
        ids.append(authed("POST", "/api/v1/offerings", token, json=body).json()["data"]["id"])
    for color in ("steelblue", "slategray"):  # two photos on the first item
        authed(
            "POST",
            f"/api/v1/offerings/{ids[0]}/images",
            token,
            files={"file": ("sneaker.jpg", jpeg(color), "image/jpeg")},
        )
    return ids


def setup_sales(token):
    authed(
        "PATCH",
        "/api/v1/sales/config",
        token,
        json={
            "agent_enabled": True,
            "lead_capture_enabled": True,
            "quotation_enabled": True,
            "notify_channels": {"email": False, "slack": False, "in_app": True, "crm": False},
        },
    )
    authed(
        "PUT",
        "/api/v1/tenant/ai-config",
        token,
        json={"first_responder": "ai", "grounding_requirement": False},
    )


def chat(name, tenant_id, text, *, user, session_id=None, note=""):
    body = {"message": text, "tenant_id": tenant_id, "user_identifier": user}
    if session_id:
        body["session_id"] = session_id
    r = record(name, "POST", "/api/v1/chat/message", body=body, note=note)
    r.raise_for_status()
    return r.json()


def main():
    from scripts.seed_data import create_subscription_plans

    from app.db.session import SessionLocal

    s = SessionLocal()
    try:
        create_subscription_plans(s)
    finally:
        s.close()

    tenant_id, email = make_tenant("alpha")
    token = login(email)
    seed_catalog(token)
    setup_sales(token)
    other_id, other_email = make_tenant("beta")
    other_token = login(other_email)
    seed_catalog(other_token)
    authed(
        "PUT",
        "/api/v1/tenant/ai-config",
        other_token,
        json={"first_responder": "ai", "grounding_requirement": False},
    )  # support mode: no sales agent

    # --- web chat (sales mode) -------------------------------------------------------------
    chat(
        "web-browse",
        tenant_id,
        "What do you have?",
        user="web-1",
        note="Browse: the menu. `meta.offerings.mode` is `browse`; items are what the reply names.",
    )
    chat(
        "web-detail-with-photos",
        tenant_id,
        "how much is the black sneaker?",
        user="web-2",
        note="Detail: one item with two photos. `cover_url` is the 400px card rendition.",
    )
    chat("web-sold-out", tenant_id, "Do you have the beach sandals?", user="web-3")
    chat(
        "web-not-listed",
        tenant_id,
        "Do you sell helicopters?",
        user="web-4",
        note="Nothing matches: `mode` is `none`, no cards; the term lands in the Not-found list.",
    )
    record(
        "staff-misses",
        "GET",
        "/api/v1/offerings/misses",
        token=token,
        note="The normalised term only: no session, no customer, no raw message.",
    )
    chat(
        "web-haggle-listed-price-stands",
        tenant_id,
        "Can you do ₦10,000 for the black sneaker?",
        user="web-5",
        note="The stand-in first answers with a concession; the guard blocks it and the "
        "regenerated reply is clean. Look at `meta.offerings.guard`.",
    )
    chat(
        "web-haggle-fallback",
        tenant_id,
        "my final offer is ₦9,000 for the white sneaker",
        user="web-6",
        note="Both attempts break the rules: the reply is built from the rows, "
        "`fell_back` is true and a human is offered (`escalation_action`).",
    )
    offer = chat(
        "web-negotiable-offer",
        tenant_id,
        "I'll pay 30k for the leather bag",
        user="web-7",
        note="Negotiable item: the offer is recorded on the lead (`interests[].offered_price`) "
        "and a human is offered. No quote while an offer is pending.",
    )
    offer_lead = db_value("SELECT id FROM leads WHERE conversation_id = :c", c=offer["session_id"])
    record(
        "staff-lead-detail-with-offer",
        "GET",
        f"/api/v1/leads/{offer_lead}",
        token=token,
        note="The customer's figure is stored next to the listed price; the AI never agreed to it.",
    )

    # --- cart -> lead -> quote -> owner alert ----------------------------------------------
    first = chat(
        "web-cart-first",
        tenant_id,
        "I want 2 black sneakers",
        user="web-8",
        note="First committed cart: lead + wanted items + catalog quote + two alerts.",
    )
    sid = first["session_id"]
    chat(
        "web-cart-changed",
        tenant_id,
        "and one white one",
        user="web-8",
        session_id=sid,
        note="The cart changed, so a new quote is made; no second owner alert.",
    )
    chat(
        "web-cart-unchanged",
        tenant_id,
        "yes that is right",
        user="web-8",
        session_id=sid,
        note="Same cart again: no new quote (no `meta.quote`).",
    )
    chat(
        "web-cart-from-priced-line",
        tenant_id,
        "I'll take the phone repair",
        user="web-9",
        note="A `from` line: wanted items are recorded, but no quote (a human prices it).",
    )

    lead_id = db_value("SELECT id FROM leads WHERE conversation_id = :c", c=sid)
    record(
        "staff-lead-detail-with-interests",
        "GET",
        f"/api/v1/leads/{lead_id}",
        token=token,
        note="`data.interests` is what the AI wrote; `source` follows the channel.",
    )
    record(
        "staff-notifications",
        "GET",
        "/api/v1/notifications",
        token=token,
        note="In-app notifications: `offering_request` on the first cart, `quote_generated`.",
    )

    # --- support-mode tenant ----------------------------------------------------------------
    chat(
        "support-mode-detail",
        other_id,
        "how much is the black sneaker?",
        user="web-10",
        note="Sales Agent off: grounded answer and cards, but no lead, cart or alert.",
    )
    save(
        "support-mode-no-lead",
        {
            "leads_for_tenant": db_value(
                "SELECT count(*) FROM leads WHERE tenant_id = :t", t=other_id
            )
        },
        note="Counted in the database: no lead was created for the support-mode tenant.",
    )

    # --- WhatsApp (fake sidecar) ------------------------------------------------------------
    consent = record("wa-consent", "GET", "/api/v1/whatsapp/consent", token=token).json()
    created = record(
        "wa-create-connection",
        "POST",
        "/api/v1/whatsapp/connections",
        token=token,
        body={
            "phone_number": f"234800{uuid.uuid4().int % 10**7:07d}",
            "label": "Shop line",
            "consent": {"version": consent["version"], "accepted": True},
        },
    ).json()
    cid = created["id"]
    key = db_value("SELECT session_key FROM whatsapp_connections WHERE id = :i", i=cid)
    http.post(f"{FAKE}/_fake/status/{key}", json={"status": "connecting"})
    http.post(f"{FAKE}/_fake/status/{key}", json={"status": "connected"})
    wait_for(
        lambda: http.get(
            f"{API}/api/v1/whatsapp/connections/{cid}", headers={"Authorization": f"Bearer {token}"}
        )
        .json()
        .get("status")
        == "connected",
        "connected",
    )

    def emit(message_id, text):
        http.post(
            f"{FAKE}/_fake/emit/{key}",
            json={
                "type": "message.inbound",
                "message_id": message_id,
                "from_ref": "198765432111@lid",
                "from_phone_digits": None,
                "push_name": "Ada",
                "kind": "text",
                "text": text,
            },
        ).raise_for_status()

    def sent():
        return http.get(FAKE + "/_fake/sent").json()

    base = len(sent())
    emit("FAKEWA0001", "What do you have?")
    wait_for(lambda: len(sent()) >= base + 1, "first WhatsApp reply")
    emit("FAKEWA0002", "I want 2 black sneakers")
    wait_for(lambda: len(sent()) >= base + 2, "second WhatsApp reply")
    time.sleep(1.5)  # let any further send land before the capture
    redacted = [{**m, "key": "<session-key-placeholder>"} for m in sent()[base:]]
    save(
        "wa-sidecar-received",
        {"fake_sidecar_received": redacted},
        note="What the fake sidecar received. Plain text (no markdown) for WhatsApp. Photos and the "
        "quote text message are a separate stream (S4b) and are not in this branch.",
    )
    wa_sid = wait_for(
        lambda: db_value(
            "SELECT session_id FROM whatsapp_chats WHERE connection_id = :c LIMIT 1", c=cid
        ),
        "chat",
    )
    record(
        "wa-staff-session-detail",
        "GET",
        f"/api/v1/chat/sessions/{wa_sid}",
        token=token,
        note="Per-message `meta.channel` is `whatsapp`; the assistant messages carry "
        "`meta.offerings` (and `meta.quote`) as on the web.",
    )
    wa_lead = db_value("SELECT id FROM leads WHERE conversation_id = :c", c=wa_sid)
    record(
        "wa-staff-lead-detail",
        "GET",
        f"/api/v1/leads/{wa_lead}",
        token=token,
        note="`source` is `whatsapp` (it used to be hard-coded `chat_widget`).",
    )
    # --- an offer made after the cart: a human takes over and the owner is alerted per offer ---
    after = chat(
        "web-offer-after-cart",
        tenant_id,
        "I'll pay 30k for the leather bag",
        user="web-8",
        session_id=sid,
        note="The cart already exists. The offer is recorded, no new quote is made while it is "
        "pending, a human takes over (`should_escalate`, `ticket_id`) and the owner gets a "
        "separate `Price offer` alert (the once-per-lead cart alert would have swallowed it).",
    )
    record(
        "staff-notifications-after-offer",
        "GET",
        "/api/v1/notifications",
        token=token,
        note="Newest first: the `Price offer` alert for the offer made after the cart.",
    )
    assert after["should_escalate"] is True
    # --- the model typing a price instead of its token (the guard and its fallback) -------------
    chat(
        "web-model-typed-a-price",
        tenant_id,
        "what is the exact price of the white sneaker?",
        user="web-11",
        note="The stand-in typed a literal price first. The guard rejected it, the regenerated "
        "reply used the token, and the customer sees only the live price.",
    )
    chat(
        "web-guard-fallback",
        tenant_id,
        "what is the final exact price of the black sneaker?",
        user="web-12",
        note="Both attempts typed a price: the reply is built from the rows, `fell_back` is true "
        "and the team is offered (`escalation_action` `offer_escalation`).",
    )
    # --- bargaining written as a bare number, and an ordinary question that only looks like it ----
    chat(
        "web-bare-number-offer",
        tenant_id,
        "black sneaker abeg 9000",
        user="web-13",
        note="A bare figure below the price is an offer: code answers (firm price), no model text.",
    )
    chat(
        "web-ordinary-question",
        tenant_id,
        "can you do delivery to Lekki for the black sneaker?",
        user="web-14",
        note="Looks like 'can you do' but names no figure: the model answers, the guard checks it.",
    )
    chat(
        "web-figure-only-offer",
        tenant_id,
        "I fit pay 18k",
        user="web-15",
        note="A figure with no item named and nothing in play: an offer candidate for a human "
        "(consent step, `escalation_action` `offer_escalation`), never agreed by the model.",
    )
    print("done:", _n["i"], "captures in", OUT)


if __name__ == "__main__":
    main()
