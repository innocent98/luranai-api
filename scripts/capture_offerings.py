"""Capture REAL responses of the Offerings catalog API (S1) against a RUNNING app.

Writes docs/fe-integration-captures/offerings/. Everything quoted in
docs/fe-integration-guide-offerings.md part 1 is copied from these files; re-run to re-derive.

Local, throwaway infrastructure only (never staging/prod): a Postgres migrated to head
(`alembic upgrade head`), Redis, the API and the arq worker started with the stubs of
`tests/fakes/live_stub.py` (the real photo code runs against a fake Cloudinary uploader and a
stand-in moderation; the LLM is never called), and CAPTURE_API pointing at the API:

    python -m tests.fakes.live_stub api --port 8591
    python -m tests.fakes.live_stub worker
    CAPTURE_API=http://127.0.0.1:8591 python scripts/capture_offerings.py

Authorization headers are replaced by placeholders and throwaway passwords are never written.
Rows the AI path will write later (Not-found terms, a lead's wanted items) do not exist yet in this
stream, so the script inserts them with SQL and each such capture says so in its `note`.
"""

import asyncio
import io
import json
import os
import sys
import uuid
from pathlib import Path

import httpx
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
OUT = ROOT / "docs" / "fe-integration-captures" / "offerings"
API = os.environ.get("CAPTURE_API", "http://127.0.0.1:8591")
PASSWORD = "Throwaway-Passw0rd!-local"  # local DB only; never written to a capture

OUT.mkdir(parents=True, exist_ok=True)
for old in OUT.glob("*.json"):
    old.unlink()
http = httpx.Client(timeout=60)
_n = {"i": 0}
O = "/api/v1/offerings"


def _write(name, payload):
    _n["i"] += 1
    (OUT / f"{_n['i']:02d}-{name}.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    )
    print(f"{_n['i']:02d} {name}")


def record(
    name,
    method,
    path,
    *,
    token=None,
    body=None,
    params=None,
    files=None,
    note="",
    request_note=None,
):
    """Call the API and save the exact exchange (multipart/oversized bodies are described)."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    r = http.request(method, API + path, headers=headers, json=body, params=params, files=files)
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
        request["body"] = request_note if request_note else body
    if files:
        field, (filename, content, ctype) = next(iter(files.items()))
        request["headers"]["Content-Type"] = "multipart/form-data"
        request["multipart"] = {
            field: {"filename": filename, "content_type": ctype, "size_bytes": len(content)}
        }
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


# --- images ---------------------------------------------------------------------------------


def image(fmt="JPEG", size=(160, 120), color="steelblue"):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format=fmt)
    return buf.getvalue()


# --- database helpers (throwaway DB) --------------------------------------------------------


def db_exec(sql, **params):
    from sqlalchemy import text

    from app.db.session import SessionLocal

    s = SessionLocal()
    try:
        s.execute(text(sql), params)
        s.commit()
    finally:
        s.close()


def db_rows(sql, **params):
    from sqlalchemy import text

    from app.db.session import SessionLocal

    s = SessionLocal()
    try:
        return [dict(r._mapping) for r in s.execute(text(sql), params)]
    finally:
        s.close()


def make_tenant(tag, tier, role="tenant_admin"):
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
            TenantCreate(name=f"of_{tag}_{uuid.uuid4().hex[:6]}", display_name=f"Acme {tag} Co")
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
        if tier != "trial":
            row = s.execute(select(Tenant).where(Tenant.id == tenant_id)).scalar_one()
            SubscriptionService(s)._update_capabilities_for_tier(row.id, tier)
            row.subscription_tier = tier
            row.subscription_status = "active"
            s.commit()
    finally:
        s.close()
    return str(tenant_id), email


def make_agent(tenant_id):
    from app.db.models.user import UserRole
    from app.db.session import SessionLocal
    from app.schemas.user import UserCreate
    from app.services.auth_service import AuthService

    s = SessionLocal()
    try:
        email = f"agent-{uuid.uuid4().hex[:6]}@example.com"
        AuthService(s).create_user(
            UserCreate(
                email=email,
                full_name="Support Agent",
                role=UserRole.SUPPORT_AGENT,
                password=PASSWORD,
                tenant_id=uuid.UUID(tenant_id),
            )
        )
        s.commit()
        return email
    finally:
        s.close()


def login(email, path="/api/v1/auth/login"):
    r = http.post(API + path, json={"email": email, "password": PASSWORD})
    r.raise_for_status()
    return r.json()["access_token"]


def item(**kw):
    values = {
        "kind": "product",
        "name": "Canvas sneaker",
        "price_mode": "fixed",
        "price": 12000,
        "availability": "in_stock",
    }
    values.update(kw)
    return values


def lines(prefix, n, start=1):
    return "".join(f"{prefix} {i} - {1000 + i}\n" for i in range(start, start + n))


def main():
    from scripts.seed_data import create_subscription_plans

    from app.db.session import SessionLocal

    s = SessionLocal()
    create_subscription_plans(s)
    s.close()

    tenant_id, email = make_tenant("trial", "trial")
    token = login(email)
    agent = login(make_agent(tenant_id))

    # --- auth and the plan meter ------------------------------------------------------------
    record("list-unauthenticated", "GET", O, note="No token.")
    record(
        "create-as-support-agent",
        "POST",
        O,
        token=agent,
        body=item(),
        note="Reads are open to any staff role; writes need a tenant admin.",
    )
    record("list-as-support-agent", "GET", O, token=agent, note="A support agent can read.")
    record(
        "entitlements-empty",
        "GET",
        "/api/v1/tenant/entitlements",
        token=token,
        note="The `offerings` entry is the plan meter: limit 25 on Trial, used 0.",
    )
    record(
        "settings-default",
        "GET",
        O + "/settings",
        token=token,
        note="No settings row exists yet; the defaults are returned and nothing is written.",
    )

    # --- create and validation --------------------------------------------------------------
    sneaker = record(
        "create-product",
        "POST",
        O,
        token=token,
        body=item(
            description="Black, sizes 40-45",
            category="Shoes",
            keywords="trainers, sneakers",
            negotiable=True,
        ),
        note="201 with `seq` 1 (the owner sees #1).",
    ).json()["data"]
    record(
        "create-service-from-price",
        "POST",
        O,
        token=token,
        body=item(
            kind="service",
            name="Haircut",
            price_mode="from",
            price=3000,
            price_note="per session",
            availability="limited",
            quantity=4,
            category="Services",
        ),
        note="`from` price with a note; availability `limited` with a quantity.",
    )
    record(
        "create-on-request",
        "POST",
        O,
        token=token,
        body=item(
            kind="service",
            name="Wedding styling",
            price_mode="on_request",
            price=None,
            availability="on_request",
        ),
        note="`on_request` takes no price; `price` comes back null.",
    )
    record(
        "create-price-required",
        "POST",
        O,
        token=token,
        body=item(price=None),
        note="A fixed or from item needs a price.",
    )
    record(
        "create-price-not-allowed",
        "POST",
        O,
        token=token,
        body=item(price_mode="on_request", price=500),
        note="An on_request item takes no price.",
    )
    record(
        "create-price-out-of-range",
        "POST",
        O,
        token=token,
        body=item(price=0),
        note="0 < price <= 10,000,000,000 whole naira.",
    )
    record(
        "create-several-errors",
        "POST",
        O,
        token=token,
        body=item(name="  ", price=None),
        note="Every violation comes back together in `errors`.",
    )
    record(
        "create-wrong-type-fastapi-shape",
        "POST",
        O,
        token=token,
        body={**item(), "kind": "gadget", "price": "abc"},
        note="FE TRAP: a wrong TYPE (not a business rule) is refused by the framework with its "
        "own `detail` array, not the flat {code, message, errors} body.",
    )

    # --- photos -----------------------------------------------------------------------------
    sid = sneaker["id"]
    jpeg = image()
    up1 = record(
        "photo-upload-first",
        "POST",
        f"{O}/{sid}/images",
        token=token,
        files={"file": ("front.jpg", jpeg, "image/jpeg")},
        note="201; the first photo is the cover (position 0).",
    ).json()["data"]
    record(
        "photo-upload-second",
        "POST",
        f"{O}/{sid}/images",
        token=token,
        files={"file": ("side.png", image("PNG"), "image/png")},
        note="Appended at position 1.",
    )
    record(
        "get-with-photos",
        "GET",
        f"{O}/{sid}",
        token=token,
        note="`images` ordered by position; `cover_url` is the 400 px thumbnail of the cover.",
    )
    two = record(
        "photo-upload-third",
        "POST",
        f"{O}/{sid}/images",
        token=token,
        files={"file": ("back.webp", image("WEBP"), "image/webp")},
    ).json()["data"]
    ids = [
        i["id"]
        for i in http.get(API + f"{O}/{sid}", headers={"Authorization": f"Bearer {token}"}).json()[
            "data"
        ]["images"]
    ]
    record(
        "photo-reorder",
        "PUT",
        f"{O}/{sid}/images/order",
        token=token,
        body={"image_ids": [ids[2], ids[0], ids[1]]},
        note="The first id becomes the cover; the whole item comes back.",
    )
    record(
        "photo-reorder-mismatch",
        "PUT",
        f"{O}/{sid}/images/order",
        token=token,
        body={"image_ids": [ids[0]]},
        note="The list must name every photo exactly once.",
    )
    record(
        "photo-delete",
        "DELETE",
        f"{O}/{sid}/images/{up1['id']}",
        token=token,
        note="204. The next photo is promoted if the cover is deleted; positions renumber.",
    )
    record(
        "photo-delete-not-found",
        "DELETE",
        f"{O}/{sid}/images/{up1['id']}",
        token=token,
        note="Deleting the same photo again.",
    )
    record(
        "photo-refused-gif",
        "POST",
        f"{O}/{sid}/images",
        token=token,
        files={"file": ("anim.gif", image("GIF"), "image/gif")},
        note="GIF is refused here.",
    )
    record(
        "photo-refused-not-an-image",
        "POST",
        f"{O}/{sid}/images",
        token=token,
        files={"file": ("notes.jpg", b"this is not an image", "image/jpeg")},
    )
    heic = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic" + b"\x00" * 64
    record(
        "photo-refused-heic",
        "POST",
        f"{O}/{sid}/images",
        token=token,
        files={"file": ("IMG_0001.HEIC", heic, "image/heic")},
        note="Request bytes are a minimal HEIC header, enough for the clear message.",
    )
    record(
        "photo-too-large",
        "POST",
        f"{O}/{sid}/images",
        token=token,
        files={"file": ("big.jpg", image() + b"\x00" * (5 * 1024 * 1024), "image/jpeg")},
        note="Just over 5 MB.",
    )
    record(
        "photo-blocked-by-moderation",
        "POST",
        f"{O}/{sid}/images",
        token=token,
        files={"file": ("flag.png", image("PNG", (7, 7)), "image/png")},
        note="The stand-in moderation flags a 7x7 image. The reason is never returned.",
    )
    record(
        "photo-upload-refill",
        "POST",
        f"{O}/{sid}/images",
        token=token,
        files={"file": ("detail.jpg", image(color="tomato"), "image/jpeg")},
        note="One photo was deleted above, so the item held 2; this makes 3.",
    )
    record(
        "photo-upload-up-to-the-cap",
        "POST",
        f"{O}/{sid}/images",
        token=token,
        files={"file": ("detail2.jpg", image(color="gold"), "image/jpeg")},
        note="The 4th photo: the Trial limit per item.",
    )
    record(
        "photo-upload-over-the-cap",
        "POST",
        f"{O}/{sid}/images",
        token=token,
        files={"file": ("one-more.jpg", jpeg, "image/jpeg")},
        note="409 offering_image_limit (Trial: 4 per item), refused before any moderation or "
        "upload work.",
    )

    # --- list, get, patch, archive ----------------------------------------------------------
    record(
        "list-default",
        "GET",
        O,
        token=token,
        note="Active items, newest first. `meta.limit/used` is the plan meter; "
        "`page_size` is the page size.",
    )
    record(
        "list-filter-kind-and-search",
        "GET",
        O,
        token=token,
        params={"kind": "service", "q": "hair"},
    )
    first = record("list-page-1-limit-1", "GET", O, token=token, params={"limit": 1}).json()
    record(
        "list-page-2-with-cursor",
        "GET",
        O,
        token=token,
        params={"limit": 1, "cursor": first["meta"]["next_cursor"]},
        note="Pass `meta.next_cursor` back as `cursor`; null means the last page.",
    )
    record("list-bad-cursor", "GET", O, token=token, params={"cursor": "nope"})
    record("get-one", "GET", f"{O}/{sid}", token=token)
    record("get-not-found", "GET", f"{O}/00000000-0000-0000-0000-000000000000", token=token)
    record(
        "patch-sold-out",
        "PATCH",
        f"{O}/{sid}",
        token=token,
        body={"availability": "out_of_stock"},
        note="The one-tap sold-out control. `availability_changed_at` moves.",
    )
    record(
        "patch-price-mode-to-on-request",
        "PATCH",
        f"{O}/{sid}",
        token=token,
        body={"price_mode": "on_request"},
        note="The old price is shed (null).",
    )
    record(
        "patch-back-to-fixed-needs-a-price",
        "PATCH",
        f"{O}/{sid}",
        token=token,
        body={"price_mode": "fixed"},
    )
    record(
        "patch-fixed-with-price",
        "PATCH",
        f"{O}/{sid}",
        token=token,
        body={"price_mode": "fixed", "price": 14000, "availability": "in_stock"},
    )
    record(
        "patch-clear-category-with-null",
        "PATCH",
        f"{O}/{sid}",
        token=token,
        body={"category": None},
        note="Explicit null clears; an absent field is unchanged.",
    )
    extra = record(
        "create-for-archive", "POST", O, token=token, body=item(name="Old stock")
    ).json()["data"]
    record(
        "archive-with-delete",
        "DELETE",
        f"{O}/{extra['id']}",
        token=token,
        note="204. Without ?hard=true the item is archived (not counted toward the cap).",
    )
    record("list-archived", "GET", O, token=token, params={"status": "archived"})
    record(
        "unarchive-with-patch",
        "PATCH",
        f"{O}/{extra['id']}",
        token=token,
        body={"status": "active"},
    )
    record(
        "hard-delete",
        "DELETE",
        f"{O}/{extra['id']}",
        token=token,
        params={"hard": "true"},
        note="204. Row and photos are gone; its #seq is never reused.",
    )
    record(
        "hard-delete-again-not-found",
        "DELETE",
        f"{O}/{extra['id']}",
        token=token,
        params={"hard": "true"},
    )
    record(
        "create-after-hard-delete-seq-moves-on",
        "POST",
        O,
        token=token,
        body=item(name="After delete"),
        note="`seq` continues; the deleted number is not reused.",
    )

    # --- import -----------------------------------------------------------------------------
    csv_text = (
        "name,kind,price,availability,category,external_ref\n"
        "Tote bag,product,8500,in stock,Bags,SKU-TOTE\n"
        "Braiding,service,from 5k,limited,Services,SKU-BRAID\n"
        "Bad price,product,abc,in stock,Bags,SKU-BAD\n"
    )
    record(
        "import-preview-csv",
        "POST",
        O + "/import/preview",
        token=token,
        body={"format": "csv", "content": csv_text, "mode": "add"},
        note="Nothing is saved. Row numbers count data rows (header and blank lines excluded).",
    )
    record(
        "import-preview-lines",
        "POST",
        O + "/import/preview",
        token=token,
        body={
            "format": "lines",
            "content": "Leather belt - 4500 - in stock\nTailoring - from 2k\n",
            "mode": "add",
        },
        note="The paste-from-WhatsApp format.",
    )
    record(
        "import-apply-atomic-refused",
        "POST",
        O + "/import",
        token=token,
        body={"format": "csv", "content": csv_text, "mode": "add"},
        note="One bad row refuses the whole import (nothing saved).",
    )
    record(
        "import-apply-skip-errors",
        "POST",
        O + "/import",
        token=token,
        body={"format": "csv", "content": csv_text, "mode": "add", "skip_errors": True},
        note="Valid rows are created; the bad row is reported.",
    )
    upsert_csv = (
        "name,price,external_ref\nTote bag (new price),9000,SKU-TOTE\nFresh item,100,SKU-NEW\n"
    )
    record(
        "import-preview-upsert",
        "POST",
        O + "/import/preview",
        token=token,
        body={"format": "csv", "content": upsert_csv, "mode": "upsert"},
        note="`target` names the item an update row will change.",
    )
    record(
        "import-apply-upsert",
        "POST",
        O + "/import",
        token=token,
        body={"format": "csv", "content": upsert_csv, "mode": "upsert"},
    )
    record(
        "import-unreadable",
        "POST",
        O + "/import/preview",
        token=token,
        body={"format": "csv", "content": "price,category\n100,Bags\n", "mode": "add"},
        note="A CSV needs a header with a `name` column.",
    )
    many = "name,price\n" + "".join(f"Row {i},100\n" for i in range(501))
    record(
        "import-too-many-rows",
        "POST",
        O + "/import/preview",
        token=token,
        body={"format": "csv", "content": many, "mode": "add"},
        request_note={
            "format": "csv",
            "mode": "add",
            "content": "<header + 501 generated rows 'Row N,100', trimmed here>",
        },
    )

    # --- the cap ----------------------------------------------------------------------------
    used = http.get(API + O, headers={"Authorization": f"Bearer {token}"}).json()["meta"]["used"]
    fill = 25 - used
    http.post(
        API + O + "/import",
        headers={"Authorization": f"Bearer {token}"},
        json={"format": "lines", "content": lines("Filler", fill), "mode": "add"},
    ).raise_for_status()
    record(
        "entitlements-at-the-cap",
        "GET",
        "/api/v1/tenant/entitlements",
        token=token,
        note="used 25 of 25.",
    )
    record(
        "create-over-the-cap",
        "POST",
        O,
        token=token,
        body=item(name="One too many"),
        note="409 offering_limit with `limit` and `used`.",
    )
    record(
        "import-over-the-cap",
        "POST",
        O + "/import",
        token=token,
        body={"format": "lines", "content": lines("Extra", 3), "mode": "add"},
        note="`can_add` says how many would fit; nothing is saved.",
    )
    record(
        "import-preview-over-the-cap",
        "POST",
        O + "/import/preview",
        token=token,
        body={"format": "lines", "content": lines("Extra", 3), "mode": "add"},
        note="The preview warns with `summary.over_limit_by`.",
    )

    # --- confirm and settings ---------------------------------------------------------------
    record(
        "confirm-today",
        "POST",
        O + "/confirm",
        token=token,
        note='"Everything is still correct today".',
    )
    record("settings-after-confirm", "GET", O + "/settings", token=token)
    record(
        "settings-patch",
        "PATCH",
        O + "/settings",
        token=token,
        body={"stale_after_days": 3, "currency": "ngn"},
        note="Currency is upper-cased.",
    )
    record(
        "settings-validation",
        "PATCH",
        O + "/settings",
        token=token,
        body={"stale_after_days": 400, "currency": "NAIRA"},
    )

    # --- Not-found terms (rows inserted by SQL: the AI path writes them in a later stream) ----
    db_exec(
        "INSERT INTO offering_misses (id, tenant_id, term, count, first_seen_at, last_seen_at) "
        "VALUES (:a, :t, 'red heels', 5, now() - interval '3 days', now() - interval '1 day'), "
        "(:b, :t, 'gym bag', 9, now() - interval '5 days', now() - interval '2 hours'), "
        "(:c, :t, 'old request', 40, now() - interval '70 days', now() - interval '60 days')",
        a=str(uuid.uuid4()),
        b=str(uuid.uuid4()),
        c=str(uuid.uuid4()),
        t=tenant_id,
    )
    misses = record(
        "misses-list",
        "GET",
        O + "/misses",
        token=token,
        note="ROWS INSERTED BY SQL (the AI path writes them in a later stream). "
        "Default window 30 days, most wanted first.",
    ).json()["data"]
    record("misses-list-90-days", "GET", O + "/misses", token=token, params={"days": 90})
    record(
        "misses-window-out-of-range",
        "GET",
        O + "/misses",
        token=token,
        params={"days": 91},
        note="FE TRAP: framework 422 shape.",
    )
    record("misses-dismiss", "DELETE", f"{O}/misses/{misses[0]['id']}", token=token)
    record("misses-dismiss-again-not-found", "DELETE", f"{O}/misses/{misses[0]['id']}", token=token)

    # --- worker: the 90-day sweep really runs in the arq worker -----------------------------
    old_id = str(uuid.uuid4())
    db_exec(
        "INSERT INTO offering_misses (id, tenant_id, term, count, first_seen_at, last_seen_at) "
        "VALUES (:i, :t, 'ancient term', 1, now() - interval '120 days', now() - interval '100 days')",
        i=old_id,
        t=tenant_id,
    )
    before = db_rows("SELECT term FROM offering_misses WHERE id = :i", i=old_id)

    async def enqueue():
        from arq import create_pool

        from app.workers.document_processor import get_redis_settings

        pool = await create_pool(get_redis_settings())
        await pool.enqueue_job("sweep_old_offering_misses")
        await pool.close()

    asyncio.run(enqueue())
    import time

    after = before
    for _ in range(40):
        after = db_rows("SELECT term FROM offering_misses WHERE id = :i", i=old_id)
        if not after:
            break
        time.sleep(0.5)
    save(
        "worker-sweep-old-misses",
        {
            "before": before,
            "after": after,
            "note": "A 100-day-old term inserted by SQL, then `sweep_old_offering_misses` enqueued "
            "on the arq queue and run by the real worker process.",
        },
        note="Proves the job is registered and runs in the worker.",
    )

    # --- lead detail with wanted items (rows inserted by SQL) -------------------------------
    lead_id = str(uuid.uuid4())
    db_exec(
        "INSERT INTO leads (id, tenant_id, status, created_at, updated_at) "
        "VALUES (:l, :t, 'new', now(), now())",
        l=lead_id,
        t=tenant_id,
    )
    for ref, name, price, qty, offered, age in (
        ("#1", "Canvas sneaker", 14000, 2, 12000, 5),
        ("#3", "Tote bag", 9000, 1, None, 1),
    ):
        db_exec(
            "INSERT INTO lead_offering_interests (id, tenant_id, lead_id, offering_id, ref, "
            "name_snapshot, price_mode_snapshot, price_snapshot, price_note_snapshot, qty, "
            "offered_price, created_at) VALUES (:i, :t, :l, NULL, :ref, :n, 'fixed', :p, NULL, "
            ":q, :o, now() - make_interval(mins => :age))",
            i=str(uuid.uuid4()),
            t=tenant_id,
            l=lead_id,
            ref=ref,
            n=name,
            p=price,
            q=qty,
            o=offered,
            age=age,
        )
    record(
        "lead-detail-with-interests",
        "GET",
        f"/api/v1/leads/{lead_id}",
        token=token,
        note="INTEREST ROWS INSERTED BY SQL (written by the AI path in a later stream). "
        "`interests` is the only new field.",
    )

    # --- lapsed tenant ----------------------------------------------------------------------
    lap_id, lap_email = make_tenant("lapsed", "lite")
    lap = login(lap_email)
    created = record("lapsed-before-create-ok", "POST", O, token=lap, body=item()).json()["data"]
    db_exec(
        "UPDATE tenants SET subscription_ends_at = now() - interval '2 days' WHERE id = :t",
        t=lap_id,
    )
    record(
        "lapsed-list-still-allowed",
        "GET",
        O,
        token=lap,
        note="Subscription ended 2 days ago: reads still work.",
    )
    record(
        "lapsed-create-refused",
        "POST",
        O,
        token=lap,
        body=item(name="Nope"),
        note="403 feature_not_in_plan (the flat entitlements body), not 402.",
    )
    record("lapsed-patch-refused", "PATCH", f"{O}/{created['id']}", token=lap, body={"name": "x"})

    # --- downgrade guard --------------------------------------------------------------------
    pro_id, pro_email = make_tenant("pro", "professional")
    pro = login(pro_email)
    h = {"Authorization": f"Bearer {pro}"}
    http.post(
        API + O + "/import",
        headers=h,
        json={"format": "lines", "mode": "add", "content": lines("Pro item", 120)},
    ).raise_for_status()
    pid = http.get(API + O, headers=h, params={"limit": 1}).json()["data"][0]["id"]
    for k in range(5):
        http.post(
            API + f"{O}/{pid}/images",
            headers=h,
            files={"file": (f"p{k}.jpg", image(color="seagreen"), "image/jpeg")},
        ).raise_for_status()
    record(
        "downgrade-blocked-by-catalog",
        "POST",
        "/api/v1/subscription/downgrade",
        token=pro,
        body={"tier": "lite", "reason": "capture"},
        note="Professional tenant with 120 active items (Lite allows 100) and an item holding "
        "5 photos (Lite allows 4): both appear as violations.",
    )


if __name__ == "__main__":
    main()
