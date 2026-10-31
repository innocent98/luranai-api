"""Capture REAL WhatsApp behaviour of catalog photos and the quote text (S4b) against a RUNNING app.

Writes docs/fe-integration-captures/offerings-whatsapp/. Re-run to re-derive every claim in
docs/sop/offerings_whatsapp_photos.md.

Local, throwaway infrastructure only (never staging/prod): Postgres migrated to head, Redis, the
fake WhatsApp sidecar, and the API + arq worker started with `tests/fakes/live_stub_offerings.py`
(the MODEL is a stand-in that reads the injected catalog block; lookup, block, guard, cart, quote,
the WhatsApp webhook, the arq jobs and the sidecar adapter are real; Cloudinary and moderation are
stubs, and no image URL is ever fetched):

    python -m tests.fakes.fake_sidecar --port 8599 --backend http://127.0.0.1:8592 \
        --secret $WHATSAPP_WEBHOOK_SECRET --token $WHATSAPP_SIDECAR_TOKEN
    python -m tests.fakes.live_stub_offerings api --port 8592
    python -m tests.fakes.live_stub_offerings worker
    CAPTURE_API=http://127.0.0.1:8592 CAPTURE_FAKE=http://127.0.0.1:8599 \
        python scripts/capture_offerings_whatsapp_photos.py            # CAPTURE_MODE=connection

Two modes, because the connection budget (2 catalog photos a minute per number, 15% of the daily
send cap) hides the per-chat hourly cap behind it. `CAPTURE_MODE=hourly` needs the API and worker
started with `WHATSAPP_CATALOG_PHOTOS_PER_MIN=1000 WHATSAPP_CATALOG_PHOTO_DAILY_PCT=100` and adds
the `hourly-*` captures without clearing the others.

All business data is fake (invented items, `example.com`, invented phone numbers). Throwaway
passwords are never written.
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
OUT = ROOT / "docs" / "fe-integration-captures" / "offerings-whatsapp"
API = os.environ.get("CAPTURE_API", "http://127.0.0.1:8592")
FAKE = os.environ.get("CAPTURE_FAKE", "http://127.0.0.1:8599")
PASSWORD = "Throwaway-Passw0rd!-local"  # local DB only; never written to a capture
SETTLE_S = 4.0  # photos and the quote text are deferred 1-4 s; wait this long for stragglers

MODE = os.environ.get("CAPTURE_MODE", "connection")
OUT.mkdir(parents=True, exist_ok=True)
if MODE == "connection":
    for old in OUT.glob("*.json"):
        old.unlink()
http = httpx.Client(timeout=60)
_n = {"i": 0}


def save(name, payload, note=""):
    _n["i"] += 1
    prefix = f"{_n['i']:02d}" if MODE == "connection" else "hourly"
    (OUT / f"{prefix}-{name}.json").write_text(
        json.dumps({"scenario": name, "note": note, **payload}, indent=2, ensure_ascii=False) + "\n"
    )
    print(f"{_n['i']:02d} {name}")


def wait_for(predicate, what, timeout=40.0):
    end = time.time() + timeout
    while time.time() < end:
        value = predicate()
        if value:
            return value
        time.sleep(0.4)
    raise SystemExit(f"timed out waiting for {what}")


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
            TenantCreate(name=f"wap_{tag}_{uuid.uuid4().hex[:6]}", display_name=f"Acme {tag} Shoes")
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


def _item(name, price, *, availability="in_stock", **extra):
    return dict(
        kind="product",
        name=name,
        price_mode="fixed",
        price=price,
        availability=availability,
        category="Shop",
        **extra,
    )


CATALOG = [  # (body, photos)
    (_item("Canvas sneaker black", 12000, keywords="trainers"), ("steelblue", "slategray")),
    (_item("Canvas sneaker white", 13500), ("lightgray",)),
    (_item("Leather bag", 45000, negotiable=True), ()),
    (_item("Silk scarf", 6500), ("tomato",)),
    (_item("Wool cap", 4000), ("seagreen",)),
    (_item("Beach sandals", 8000, availability="out_of_stock"), ("khaki",)),
]


def seed(token):
    ids = {}
    for body, colours in CATALOG:
        item_id = authed("POST", "/api/v1/offerings", token, json=body).json()["data"]["id"]
        ids[body["name"]] = item_id
        for colour in colours:
            authed(
                "POST",
                f"/api/v1/offerings/{item_id}/images",
                token,
                files={"file": ("photo.jpg", jpeg(colour), "image/jpeg")},
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


def sent():
    return http.get(FAKE + "/_fake/sent").json()


def main():
    tenant_id, email = make_tenant("a")
    token = login(email)
    ids = seed(token)
    setup_sales(token)
    consent = http.get(
        API + "/api/v1/whatsapp/consent", headers={"Authorization": f"Bearer {token}"}
    ).json()
    created = http.post(
        API + "/api/v1/whatsapp/connections",
        headers={"Authorization": f"Bearer {token}"},
        json={
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

    def emit(ref, text):
        http.post(
            f"{FAKE}/_fake/emit/{key}",
            json={
                "type": "message.inbound",
                "message_id": f"FAKEWA{uuid.uuid4().hex[:10]}",
                "from_ref": ref,
                "from_phone_digits": None,
                "push_name": "Ada",
                "kind": "text",
                "text": text,
            },
        ).raise_for_status()

    def say(ref, text, *, expect_at_least):
        """Send one customer message; return what the sidecar received for it, in order."""
        base = len(sent())
        emit(ref, text)
        wait_for(lambda: len(sent()) >= base + expect_at_least, f"replies to {text!r}")
        time.sleep(SETTLE_S)
        return [{**m, "key": "<session-key-placeholder>"} for m in sent()[base:]]

    def shape(received):
        return [("image" if "image" in m else "text") for m in received]

    def redis_count(kind, ident):
        from redis import Redis

        r = Redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
        return r.zcard(f"wa:catphotos:{kind}:{ident}")

    chat_a = "198765432111@lid"
    chat_b = "198765432222@lid"

    if MODE == "hourly":
        rounds = []
        for text in [
            "tell me about the black sneaker",
            "tell me about the white sneaker",
            "tell me about the silk scarf",
            "tell me about the wool cap",
            "tell me about the black sneaker",
            "tell me about the white sneaker",
            "tell me about the silk scarf",
        ]:
            got = say(chat_b, text, expect_at_least=1)
            rounds.append({"customer_said": text, "received": shape(got), "messages": got})
        photos_b = sum(r["received"].count("image") for r in rounds)
        chat_b_id = db_value(
            "SELECT id FROM whatsapp_chats WHERE connection_id = :c LIMIT 1", c=cid
        )
        window = redis_count("chat", chat_b_id)
        save(
            "per-chat-cap-six-photos-per-hour-then-text-only",
            {
                "rounds": rounds,
                "photos_sent_to_this_chat": photos_b,
                "limiter_entries_in_redis": window,
            },
            note="Connection budgets raised out of the way for this run. Seven replies about items "
            "that all have photos: six photos go (6 per chat per hour), the seventh keeps its "
            "text and loses its photo.",
        )
        assert photos_b == 6 and window == 6, (photos_b, window)
        print("done (hourly):", _n["i"])
        return

    detail = say(chat_a, "tell me about the black sneaker", expect_at_least=2)
    save(
        "detail-text-then-photo",
        {"customer_said": "tell me about the black sneaker", "fake_sidecar_received": detail},
        note="The reply text first, then the cover photo (the WhatsApp rendition: `c_limit,w_1280,"
        "h_1280`) with a caption built at send time: `#ref name · price · availability`. The photo "
        "is queued only after the reply was sent.",
    )
    assert shape(detail) == ["text", "image"], shape(detail)

    menu = say(chat_a, "what do you have?", expect_at_least=1)
    save(
        "menu-text-only",
        {"customer_said": "what do you have?", "fake_sidecar_received": menu},
        note="More than 3 items shown is a menu: text only, no photos.",
    )
    assert "image" not in shape(menu), shape(menu)

    sold = say(chat_a, "tell me about the beach sandals", expect_at_least=1)
    save(
        "sold-out-text-only",
        {"customer_said": "tell me about the beach sandals", "fake_sidecar_received": sold},
        note="The sandals have a photo but are sold out: the AI says so, no photo is sent.",
    )
    assert "image" not in shape(sold), shape(sold)

    quote = say(chat_a, "I want 2 black sneakers", expect_at_least=2)
    save(
        "cart-reply-then-quote-text-then-photo",
        {"customer_said": "I want 2 black sneakers", "fake_sidecar_received": quote},
        note="Reply, then the quote as a plain-text message (rendered by code from the stored "
        "catalog quote), then the item photo: each is queued only after the one before it was "
        "sent. This was the connection's second catalog photo within the minute.",
    )
    assert shape(quote)[:2] == ["text", "text"] and "Total:" in quote[1].get("text", ""), quote

    conn_min = redis_count("min", cid)
    save(
        "connection-budget-in-redis",
        {"catalog_photos_sent_by_this_number_in_the_last_minute": conn_min},
        note="Per-connection sliding window read from Redis: 2 photos a minute for the number.",
    )

    second = say(chat_b, "tell me about the white sneaker", expect_at_least=1)
    save(
        "second-customer-same-minute-text-kept-photo-skipped",
        {"customer_said": "tell me about the white sneaker", "fake_sidecar_received": second},
        note="Another customer writes in the same minute: the number's photo budget is used up, so "
        "this customer's text reply goes out normally and the photo is skipped (dropped, never "
        "retried, never queued behind).",
    )
    assert shape(second) == ["text"], shape(second)

    time.sleep(65)  # the minute window slides
    later = say(chat_b, "tell me about the white sneaker", expect_at_least=2)
    save(
        "second-customer-a-minute-later-photo-goes",
        {"customer_said": "tell me about the white sneaker", "fake_sidecar_received": later},
        note="A minute later the budget has room again: text, then the photo.",
    )
    assert shape(later) == ["text", "image"], shape(later)

    save(
        "database-after",
        {
            "message_attachments_for_tenant": db_value(
                "SELECT count(*) FROM message_attachments WHERE tenant_id = :t", t=tenant_id
            ),
            "offering_images_for_tenant": db_value(
                "SELECT count(*) FROM offering_images WHERE tenant_id = :t", t=tenant_id
            ),
            "photos_the_sidecar_received_in_total": sum("image" in m for m in sent()),
            "connection_day_window_entries": redis_count("day", cid),
        },
        note="Catalog photos are never stored as `message_attachments` (the retention job deletes "
        "those and would destroy the catalog asset): the tenant has 0 attachment rows while the "
        "sidecar received the photos; the catalog rows are intact.",
    )
    print("done:", _n["i"], "captures in", OUT, "| items:", len(ids))


if __name__ == "__main__":
    main()
