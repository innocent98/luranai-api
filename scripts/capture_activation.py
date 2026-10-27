"""Capture REAL responses for docs/fe-integration-guide-onboarding.md from a running local app.

Everything in the guide is copied from the files this writes to
docs/fe-integration-captures/activation/. Re-run it to re-derive any claim.

Prerequisites (local, throwaway infrastructure only; never point this at staging/prod):
  * Postgres migrated to head, plans seeded (scripts/seed_data.py), Qdrant up
  * API on --base (default http://127.0.0.1:8582) and the arq worker (`python worker.py`)
  * optional --bad-embeddings-base: a second API instance started with an invalid OPENAI_API_KEY
    (to capture `embedding_failed`)
  * optional --qdrant-container: docker container name; it is stopped/started to capture
    `vector_store_unavailable` and the onboarding behaviour while the store is down

    poetry run python scripts/capture_activation.py --base http://127.0.0.1:8582

It registers throwaway tenants with @example.com addresses, activates them directly in the
local database and sets a throwaway password. Authorization headers, API keys and passwords are
replaced with placeholders in the written files.
"""

import argparse
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
OUT = ROOT / "docs" / "fe-integration-captures" / "activation"

PASSWORD = "Throwaway-Passw0rd!-local"  # local DB only; never written to a capture
BAKERY_FAQ = """Opening hours
We are open Monday to Saturday from 7am to 6pm, and on Sundays from 8am to 2pm.

Delivery
We deliver within 10km of the shop. Delivery costs 1,500 naira and takes about 45 minutes.

Prices
A loaf of bread is 1,200 naira. Birthday cakes start at 15,000 naira and need two days' notice.

Returns
If your order is wrong, tell us within 24 hours and we will replace it or refund you.
"""

_counter = {"n": 0}


def _scrub(value):
    """Replace anything that looks like a secret in captured JSON."""
    if isinstance(value, dict):
        return {k: _scrub_field(k, v) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return value


def _scrub_field(key, value):
    if key in {"full_key", "access_token", "refresh_token", "api_key"}:
        return "<redacted-placeholder>"
    return _scrub(value)


class Capturer:
    def __init__(self, api: str):
        self.api = api
        self.http = httpx.Client(timeout=120)

    def call(
        self,
        scenario: str,
        note: str,
        method: str,
        path: str,
        *,
        token: str | None = None,
        api_key: str | None = None,
        headers: dict | None = None,
        json_body=None,
        files=None,
        data=None,
        capture: bool = True,
        api: str | None = None,
    ) -> httpx.Response:
        req_headers = dict(headers or {})
        if token:
            req_headers["Authorization"] = f"Bearer {token}"
        if api_key:
            req_headers["X-API-Key"] = api_key
        r = self.http.request(
            method,
            (api or self.api) + path,
            headers=req_headers,
            json=json_body,
            files=files,
            data=data,
        )
        if capture:
            _counter["n"] += 1
            shown = {k: v for k, v in (headers or {}).items()}
            if token:
                shown["Authorization"] = "Bearer <admin-jwt-placeholder>"
            if api_key:
                shown["X-API-Key"] = "<public-api-key-placeholder>"
            try:
                body = _scrub(r.json())
            except ValueError:
                body = r.text
            req_body = json_body
            if files:
                req_body = {"multipart_file": {k: v[0] for k, v in files.items()}, **(data or {})}
            record = {
                "scenario": scenario,
                "note": note,
                "request": {
                    "method": method,
                    "path": path.replace(api_key or "\0", "<key>"),
                    "headers": shown,
                    "body": req_body,
                },
                "response": {"status": r.status_code, "body": body},
            }
            name = f"{_counter['n']:02d}-{scenario}.json"
            OUT.mkdir(parents=True, exist_ok=True)
            (OUT / name).write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n")
            print(f"  {name}: {method} {path.split('?')[0]} -> {r.status_code}")
        return r


def db_activate(email: str, caps: dict | None = None) -> uuid.UUID:
    """Activate the registered admin, set the throwaway password, optionally widen the caps."""
    from sqlalchemy import select

    from app.core.security import get_password_hash
    from app.db.models.tenant import TenantCapabilities
    from app.db.models.user import User
    from app.db.session import SessionLocal

    db = SessionLocal()
    try:
        user = next(u for u in db.execute(select(User)).scalars() if u.email == email)
        user.is_active = True
        if hasattr(user, "is_verified"):
            user.is_verified = True
        user.hashed_password = get_password_hash(PASSWORD)
        if caps:
            capab = db.execute(
                select(TenantCapabilities).where(TenantCapabilities.tenant_id == user.tenant_id)
            ).scalar_one()
            for k, v in caps.items():
                setattr(capab, k, v)
        db.commit()
        return user.tenant_id
    finally:
        db.close()


def register_and_login(cap: Capturer, label: str, caps: dict | None = None):
    email = f"activation-{label}-{uuid.uuid4().hex[:6]}@example.com"
    r = cap.call(
        "register",
        "setup",
        "POST",
        "/auth/register-company",
        json_body={
            "company_name": f"Demo Bakery {label} {uuid.uuid4().hex[:6]}",
            "admin_full_name": "Demo Owner",
            "admin_email": email,
        },
        capture=False,
    )
    assert r.status_code == 201, r.text
    tenant_id = db_activate(email, caps)
    r = cap.call(
        "login",
        "setup",
        "POST",
        "/auth/login",
        json_body={"email": email, "password": PASSWORD},
        capture=False,
    )
    assert r.status_code == 200, r.text
    return str(tenant_id), r.json()["access_token"]


def wait_job(cap: Capturer, token: str, job_id: str, scenario: str, note: str) -> httpx.Response:
    for _ in range(60):
        r = cap.call(scenario, note, "GET", f"/jobs/{job_id}", token=token, capture=False)
        if r.json().get("status") in {"completed", "failed"}:
            return cap.call(scenario, note, "GET", f"/jobs/{job_id}", token=token)
        time.sleep(1)
    raise SystemExit(f"job {job_id} did not finish")


def docker(action: str, container: str) -> None:
    subprocess.run(["docker", action, container], check=True, capture_output=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8582")
    ap.add_argument("--bad-embeddings-base", help="API instance started with an invalid OpenAI key")
    ap.add_argument("--qdrant-container", help="docker container to stop/start for the outage")
    ap.add_argument("--dashboard-origin", default="http://localhost:3000")
    args = ap.parse_args()
    api = args.base + "/api/v1"
    cap = Capturer(api)

    print("== tenant A (the one we walk through activation)")
    tid, tok = register_and_login(
        cap, "a", {"max_knowledge_sources": 50, "can_upload_knowledge": True}
    )

    cap.call("onboarding-fresh", "Brand-new tenant: nothing done.", "GET", "/tenant/onboarding", token=tok)

    print("== upload traps")
    for scenario, text, note in (
        ("text-upload-too-short", "hello", "Below the 20 character minimum: 422, NO source created."),
        ("text-upload-empty", "   ", "Whitespace only: same code."),
        ("text-upload-no-letters", "-" * 40, "Long enough but nothing readable."),
    ):
        cap.call(
            scenario,
            note,
            "POST",
            "/knowledge/upload",
            token=tok,
            json_body={"tenant_id": tid, "document_name": "x", "document_text": text},
        )
    cap.call(
        "sources-after-rejected-uploads",
        "Rejected uploads left no source behind.",
        "GET",
        f"/knowledge/tenants/{tid}/sources",
        token=tok,
    )

    cap.call(
        "text-upload-short-but-valid",
        "25 characters: used to fail in the chunker, now becomes one chunk and is indexed.",
        "POST",
        "/knowledge/upload",
        token=tok,
        json_body={
            "tenant_id": tid,
            "document_name": "hours-one-liner",
            "document_text": "We open at 7am every day.",
            "source_type": "faq",
        },
    )
    cap.call(
        "onboarding-knowledge-done-human-first",
        "Indexed knowledge, but this tenant was created before it had any so first_responder is human.",
        "GET",
        "/tenant/onboarding",
        token=tok,
    )
    cap.call(
        "ai-config-let-ai-answer-first",
        "Contract section 4: reuse the existing ai-config PUT, no new endpoint.",
        "PUT",
        "/tenant/ai-config",
        token=tok,
        json_body={"first_responder": "ai_with_handoff"},
    )
    cap.call(
        "onboarding-ai-will-answer",
        "blocker is null once knowledge is indexed and the AI answers first.",
        "GET",
        "/tenant/onboarding",
        token=tok,
    )
    r = cap.call(
        "text-upload-faq",
        "A multi-topic FAQ is split per topic (chunk_count 4), every chunk indexed.",
        "POST",
        "/knowledge/upload",
        token=tok,
        json_body={
            "tenant_id": tid,
            "document_name": "bakery-faq",
            "document_text": BAKERY_FAQ,
            "source_type": "faq",
        },
    )
    cap.call("sources-list", "Source list with chunk counts.", "GET", f"/knowledge/tenants/{tid}/sources", token=tok)
    faq_id = r.json()["id"]
    cap.call("source-chunks", "The four topical chunks.", "GET", f"/knowledge/sources/{faq_id}/chunks", token=tok)

    print("== file uploads (worker)")
    r = cap.call(
        "file-upload-too-short",
        "A readable but too-short file is accepted (extraction is fine) and fails in the worker.",
        "POST",
        "/knowledge/upload-file",
        token=tok,
        files={"file": ("tiny.txt", b"Open 7am daily", "text/plain")},
        data={"tenant_id": tid, "source_type": "faq"},
    )
    wait_job(
        cap, tok, r.json()["job_id"], "job-failed-document-too-short",
        "Job status exposes failure_reason; no source was created.",
    )
    r = cap.call(
        "file-upload-ok",
        "A normal file.",
        "POST",
        "/knowledge/upload-file",
        token=tok,
        files={"file": ("menu.txt", BAKERY_FAQ.encode(), "text/plain")},
        data={"tenant_id": tid, "source_type": "faq"},
    )
    wait_job(cap, tok, r.json()["job_id"], "job-completed", "Completed job: result_data, failure_reason null.")
    cap.call(
        "file-upload-no-text",
        "Nothing readable at all (e.g. a scanned PDF): rejected immediately, no job.",
        "POST",
        "/knowledge/upload-file",
        token=tok,
        files={"file": ("scan.txt", b"-----  ----- .....", "text/plain")},
        data={"tenant_id": tid, "source_type": "faq"},
    )

    print("== widget seen")
    r = cap.call(
        "api-key-create", "setup", "POST", "/api-keys", token=tok,
        json_body={"name": "capture key", "key_type": "public"}, capture=False,
    )
    assert r.status_code == 201, r.text
    key = r.json()["full_key"]
    cfg_path = f"/widget/public/config/{tid}"
    cap.call(
        "widget-config-from-dashboard-origin",
        "Luran's own origin (FRONTEND_URL) is ignored: nothing recorded.",
        "GET", cfg_path, api_key=key, headers={"Origin": args.dashboard_origin},
    )
    cap.call("onboarding-widget-not-seen", "Still not seen.", "GET", "/tenant/onboarding", token=tok)
    cap.call(
        "widget-config-from-customer-site",
        "Origin has a port, path and query on purpose: only the hostname is stored.",
        "GET", cfg_path, api_key=key,
        headers={"Origin": "https://demo-bakery.example.com:8443", "Referer": "https://demo-bakery.example.com/order?email=x@y.z"},
    )
    cap.call("onboarding-widget-seen", "widget.done, hostname only.", "GET", "/tenant/onboarding", token=tok)
    cap.call(
        "widget-config-second-site-within-10-min",
        "Throttled: a second fetch inside 10 minutes writes nothing.",
        "GET", cfg_path, api_key=key, headers={"Origin": "https://other-site.example.org"},
    )
    cap.call("onboarding-widget-throttled", "last_seen_at / host unchanged.", "GET", "/tenant/onboarding", token=tok)

    print("== test conversation")
    r = cap.call(
        "chat-first-message",
        "A real customer message answered from the knowledge base.",
        "POST",
        "/chat/message",
        json_body={
            "message": "What time do you open?",
            "tenant_id": tid,
            "user_identifier": "visitor-demo-001",
        },
    )
    cap.call("onboarding-all-done", "All three steps done.", "GET", "/tenant/onboarding", token=tok)

    print("== tenant isolation")
    tid_b, tok_b = register_and_login(cap, "b", {"max_knowledge_sources": 5, "can_upload_knowledge": True})
    cap.call(
        "onboarding-other-tenant-sees-only-itself",
        "Tenant B, same moment: zeros. Tenant A's activity is invisible to B.",
        "GET", "/tenant/onboarding", token=tok_b,
    )
    cap.call(
        "upload-into-other-tenant-forbidden",
        "B uploading into A's tenant id is refused.",
        "POST", "/knowledge/upload", token=tok_b,
        json_body={"tenant_id": tid, "document_name": "x", "document_text": "x" * 40},
    )
    cap.call("onboarding-no-auth", "No token.", "GET", "/tenant/onboarding")

    print("== legacy rows: ACTIVE in Postgres, no vectors")
    from sqlalchemy import text

    from app.db.session import SessionLocal

    db = SessionLocal()
    db.execute(
        text(
            "INSERT INTO knowledge_sources (id, tenant_id, name, source_type, source_category, "
            "content_hash, version, status, chunk_count, created_at, updated_at) VALUES "
            "(:id, :t, 'legacy-text-upload', 'FAQ', 'file', :h, 1, 'ACTIVE', 1, now(), now())"
        ),
        {"id": str(uuid.uuid4()), "t": tid_b, "h": uuid.uuid4().hex + uuid.uuid4().hex},
    )
    db.commit()
    db.close()
    cap.call(
        "onboarding-active-source-without-vectors",
        "Simulated pre-fix row (inserted directly): ACTIVE but nothing in the vector store.",
        "GET", "/tenant/onboarding", token=tok_b,
    )

    if args.qdrant_container:
        print("== vector store outage")
        docker("stop", args.qdrant_container)
        try:
            cap.call(
                "text-upload-vector-store-down",
                "Qdrant unreachable: 503, source is kept as FAILED with a reason.",
                "POST", "/knowledge/upload", token=tok,
                json_body={"tenant_id": tid, "document_name": "during-outage", "document_text": BAKERY_FAQ},
            )
            cap.call("sources-list-with-failed", "The failed source shows failure_reason.", "GET", f"/knowledge/tenants/{tid}/sources", token=tok)
            cap.call(
                "onboarding-vector-store-down",
                "Cannot verify the vectors: knowledge is NOT claimed done (no 500).",
                "GET", "/tenant/onboarding", token=tok,
            )
        finally:
            docker("start", args.qdrant_container)
            time.sleep(4)

    if args.bad_embeddings_base:
        print("== embedding failure (instance with an invalid OpenAI key)")
        bad_api = args.bad_embeddings_base + "/api/v1"
        cap.call(
            "text-upload-embedding-failed",
            "Embeddings unavailable: 502, source kept as FAILED with a reason.",
            "POST", "/knowledge/upload", token=tok, api=bad_api,
            json_body={"tenant_id": tid, "document_name": "bad-embed", "document_text": BAKERY_FAQ},
        )
        cap.call("sources-list-embedding-failed", "Listing shows failure_reason.", "GET", f"/knowledge/tenants/{tid}/sources", token=tok)

    print("== failed sources do not consume the plan cap")
    db = SessionLocal()
    from sqlalchemy import select

    from app.db.models.tenant import TenantCapabilities

    caps_row = db.execute(select(TenantCapabilities).where(TenantCapabilities.tenant_id == uuid.UUID(tid))).scalar_one()
    active_or_processing = db.execute(
        text("SELECT count(*) FROM knowledge_sources WHERE tenant_id=:t AND status IN ('ACTIVE','PROCESSING')"),
        {"t": tid},
    ).scalar()
    caps_row.max_knowledge_sources = active_or_processing  # cap == exactly the live sources
    db.commit()
    db.close()
    cap.call(
        "file-upload-at-cap",
        "At the cap with FAILED sources present: a new file is refused (cap counts live sources only).",
        "POST", "/knowledge/upload-file", token=tok,
        files={"file": ("one-more.txt", BAKERY_FAQ.encode(), "text/plain")},
        data={"tenant_id": tid, "source_type": "faq"},
    )

    print("== consent copy")
    cap.call("whatsapp-consent", "New approved copy and version.", "GET", "/whatsapp/consent", token=tok)
    print(f"\nwrote {_counter['n']} captures to {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
