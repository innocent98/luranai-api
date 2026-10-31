"""Drive the AI sales agent journey against the LOCAL stack and record real exchanges.

Run from the repo root with the smoke env loaded (see the SOP note):

    PYTHONPATH=. poetry run python docs/fe-integration-captures/sales-agent-fixes-2026-10-30/harness/drive.py

Throwaway local infrastructure only. Writes numbered request/response captures next to this
folder and a `results.json` with one verdict per check. Authorization headers are replaced by
placeholders in the written files.
"""

from __future__ import annotations

import json
import re
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

API = "http://127.0.0.1:58000/api/v1"
MAIL = "http://127.0.0.1:58025/api/v1"
OUT = Path(__file__).resolve().parent.parent
RAW = OUT / "raw"
PASSWORD = "Throwaway-Passw0rd!-local"  # local DB only

http = httpx.Client(timeout=60)
_n = {"i": 0}
CHECKS: list[dict] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    CHECKS.append({"check": name, "pass": bool(ok), "detail": detail})
    print(f"  [{'PASS' if ok else 'FAIL'}] {name} {detail}")
    return bool(ok)


def call(scenario, note, method, path, *, token=None, body=None, capture=True, base=API):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    r = http.request(method, base + path, headers=headers, json=body)
    if capture:
        _n["i"] += 1
        try:
            rb = r.json()
        except ValueError:
            rb = r.text[:500]
        rec = {
            "scenario": scenario,
            "note": note,
            "request": {
                "method": method,
                "path": path,
                "headers": {"Authorization": "Bearer <jwt-placeholder>"} if token else {},
                "body": body,
            },
            "response": {"status": r.status_code, "body": rb},
        }
        (OUT / f"{_n['i']:02d}-{scenario}.json").write_text(
            json.dumps(rec, indent=2, ensure_ascii=False, default=str) + "\n"
        )
        print(f"  {_n['i']:02d}-{scenario}: {method} {path.split('?')[0]} -> {r.status_code}")
    return r


def save(scenario, note, data):
    _n["i"] += 1
    (OUT / f"{_n['i']:02d}-{scenario}.json").write_text(
        json.dumps({"scenario": scenario, "note": note, "data": data}, indent=2, default=str)
        + "\n"
    )
    print(f"  {_n['i']:02d}-{scenario}: (db/receiver evidence)")


# ------------------------------------------------------------------ local DB helpers
def _db():
    from app.db.session import SessionLocal

    return SessionLocal()


def clear_rate_limits():
    import redis

    from app.core.config import settings

    c = redis.Redis.from_url(settings.REDIS_URL)
    for key in c.scan_iter(match="http_*", count=500):
        c.delete(key)
    c.close()


def db_activate(email: str) -> uuid.UUID:
    from sqlalchemy import select

    from app.core.security import get_password_hash
    from app.db.models.user import User

    db = _db()
    try:
        user = next(u for u in db.execute(select(User)).scalars() if u.email == email)
        user.is_active = True
        if hasattr(user, "is_verified"):
            user.is_verified = True
        user.hashed_password = get_password_hash(PASSWORD)
        db.commit()
        return user.tenant_id
    finally:
        db.close()


def db_set_tier(tenant_id, tier: str):
    from sqlalchemy import select

    from app.db.models.tenant import Tenant
    from app.services.subscription_service import SubscriptionService

    db = _db()
    try:
        t = db.execute(select(Tenant).where(Tenant.id == uuid.UUID(str(tenant_id)))).scalar_one()
        SubscriptionService(db)._update_capabilities_for_tier(t.id, tier)
        t.subscription_tier = tier
        t.subscription_status = "active"
        db.commit()
    finally:
        db.close()


def db_rows(sql: str, **params):
    from sqlalchemy import text

    db = _db()
    try:
        return [dict(r._mapping) for r in db.execute(text(sql), params)]
    finally:
        db.close()


def register(label: str, tier: str):
    clear_rate_limits()
    email = f"smoke-{label}-{uuid.uuid4().hex[:6]}@example.com"
    r = call(
        "register",
        "setup",
        "POST",
        "/auth/register-company",
        body={
            "company_name": f"Smoke {label} {uuid.uuid4().hex[:6]}",
            "admin_full_name": "Smoke Owner",
            "admin_email": email,
        },
        capture=False,
    )
    assert r.status_code == 201, r.text
    tenant_id = db_activate(email)
    db_set_tier(tenant_id, tier)
    r = call("login", "setup", "POST", "/auth/login", body={"email": email, "password": PASSWORD},
             capture=False)
    assert r.status_code == 200, r.text
    return str(tenant_id), email, r.json()["access_token"]


# ------------------------------------------------------------------ stand-in receivers / mail
def received() -> list[dict]:
    p = RAW / "hook-received.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def llm_requests() -> list[dict]:
    p = RAW / "llm-requests.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def mails() -> list[dict]:
    r = http.get(MAIL + "/messages")
    return r.json().get("messages", [])


def wait_for(pred, what: str, timeout: float = 30.0):
    end = time.time() + timeout
    while time.time() < end:
        v = pred()
        if v:
            return v
        time.sleep(0.5)
    print(f"  (timed out waiting for {what})")
    return None


def logs_for(tenant_id: str):
    return db_rows(
        "select event, channel, status, recipient, error, entity_type from notification_logs "
        "where tenant_id = :t order by created_at, channel",
        t=tenant_id,
    )


def chat(tenant_id, session_id, user_ident, text, scenario, note):
    clear_rate_limits()
    return call(
        scenario,
        note,
        "POST",
        "/chat/message",
        body={
            "session_id": session_id,
            "tenant_id": tenant_id,
            "user_identifier": user_ident,
            "message": text,
            "metadata": {"utm_source": "google_ads", "utm_medium": "cpc"},
        },
    )


def new_session(tenant_id, user_ident, scenario):
    clear_rate_limits()
    r = call(
        scenario,
        "visitor opens the chat widget",
        "POST",
        "/chat/sessions",
        body={"tenant_id": tenant_id, "user_identifier": user_ident, "metadata": {}},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


PRICING_TIERS = [
    {"name": "Starter", "min_users": 1, "max_users": 10, "base_price": 50000,
     "per_seat_overage_price": 5000},
    {"name": "Professional", "min_users": 11, "max_users": 50, "base_price": 150000,
     "per_seat_overage_price": 3000},
    {"name": "Business", "min_users": 51, "max_users": 200, "base_price": 400000,
     "per_seat_overage_price": 2000},
]
QUALIFICATION = [
    {"key": "use_case", "question": "What are you hoping to use Luran for in your business?",
     "type": "text"},
    {"key": "timeline", "question": "When would you like to get started?", "type": "text"},
]
AVAIL = {"weekdays": [1, 2, 3, 4, 5, 6, 7], "start": "09:00", "end": "17:00", "slot_minutes": 30,
         "buffer_minutes": 15, "tenant_timezone": "Africa/Lagos"}
HOOK = "http://127.0.0.1:58091"


def main() -> int:
    for p in OUT.glob("[0-9][0-9]-*.json"):
        p.unlink()
    for p in RAW.glob("*.jsonl"):
        p.unlink()
    http.delete(MAIL + "/messages")

    print("== 0. health")
    r = call("health", "API up on the local stack", "GET", "/health")
    check("api healthy", r.status_code == 200 and r.json().get("status") == "healthy")

    print("== 1. tenants (Professional = sales on; Lite = sales_close off)")
    pro_id, pro_email, pro = register("pro", "professional")
    lite_id, lite_email, lite = register("lite", "lite")
    # New tenants start with first_responder="human": the AI never replies until the admin picks
    # "ai" (docs/fe-integration-guide-who-answers-first.md). Real API, same call the dashboard makes.
    r = call("ai-config-default-new-tenant", "A brand-new tenant's default: human answers first",
             "GET", "/tenant/ai-config", token=pro)
    check("new tenant defaults to first_responder=human (AI silent)",
          r.json().get("first_responder") == "human")
    for tok, tag in ((pro, "professional"), (lite, "lite")):
        r = call(f"ai-config-set-ai-first-{tag}", "Admin switches to AI first", "PUT",
                 "/tenant/ai-config", token=tok, body={"first_responder": "ai"})
        check(f"{tag}: first_responder set to ai", r.status_code == 200
              and r.json().get("first_responder") == "ai", f"status={r.status_code}")
    r = call("entitlements-professional", "Professional plan entitlements", "GET",
             "/tenant/entitlements", token=pro)
    r2 = call("entitlements-lite", "Lite plan entitlements", "GET", "/tenant/entitlements",
              token=lite)

    print("== 2. sales config (Professional)")
    cfg_body = {
        "agent_enabled": True, "lead_capture_enabled": True, "meeting_booking_enabled": True,
        "quotation_enabled": True,
        "notify_channels": {"email": True, "slack": True, "in_app": True, "crm": True},
        "notification_email": "sales-rep@example.com",
        "slack_webhook_url": f"{HOOK}/slack", "crm_webhook_url": f"{HOOK}/crm",
        "pricing_tiers": PRICING_TIERS, "currency": "NGN", "annual_discount_pct": 20,
        "qualification_questions": QUALIFICATION, "availability": AVAIL,
    }
    r = call("sales-config-save", "Tenant enables everything and points Slack/CRM at the "
             "local stand-in receiver", "PATCH", "/sales/config", token=pro, body=cfg_body)
    check("config saved on Professional", r.status_code == 200 and r.json().get("success"))
    saved = r.json().get("data", {})
    # The earlier run's biggest bug was a config schema drift that silently dropped saves.
    check("webhook + availability + tiers round-trip (the old drift bug)",
          saved.get("crm_webhook_url") == cfg_body["crm_webhook_url"]
          and saved.get("slack_webhook_url") == cfg_body["slack_webhook_url"]
          and saved.get("availability") == AVAIL
          and saved.get("pricing_tiers") == PRICING_TIERS)
    r = call("sales-config-get", "Re-read after save", "GET", "/sales/config", token=pro)
    check("config persisted on re-read", r.json()["data"]["crm_webhook_url"] == cfg_body["crm_webhook_url"])

    print("== 3. visitor chat, buying intent")
    v1 = "smoke-visitor-1"
    sid = new_session(pro_id, v1, "chat-session-create")
    m1 = chat(pro_id, sid, v1, "Hi, how much does your product cost? We run a logistics company.",
              "chat-1-pricing-question", "Buying intent, no contact details yet")
    j = m1.json()
    check("turn 1 replied 200", m1.status_code == 200, f"reply={j.get('message', {}).get('content')!r}")
    lr = llm_requests()
    sys_prompt = lr[-1]["system_prompt"] if lr else ""
    check("directive carries the tenant's first qualification question",
          QUALIFICATION[0]["question"] in sys_prompt)
    check("directive carries the configured plans + annual discount",
          "## Available Plans" in sys_prompt and "Professional: ₦150,000" in sys_prompt
          and "Annual billing gets 20% off" in sys_prompt)
    check("directive tells the model meeting scheduling IS available",
          "Meeting scheduling IS available" in sys_prompt)
    check("reply asks the qualifying question (stand-in echoes the directive's)",
          QUALIFICATION[0]["question"] in (j.get("message", {}).get("content") or ""))
    leads = call("leads-after-turn-1", "Lead row after the first buying-intent turn", "GET",
                 "/leads", token=pro).json()["data"]
    check("lead created on first trigger-intent turn", leads["total"] == 1,
          f"intent={leads['items'][0]['intent'] if leads['items'] else None}")

    m2 = chat(pro_id, sid, v1, "We're about 40 people, mostly dispatch staff. Which plan fits?",
              "chat-2-team-size", "Qualification answered: team size + industry captured")
    lead_id = call("leads-after-turn-2", "Lead after qualification answer", "GET", "/leads",
                   token=pro).json()["data"]["items"][0]["id"]
    d = call("lead-detail-after-turn-2", "Lead detail (cold/warm, no email yet)", "GET",
             f"/leads/{lead_id}", token=pro).json()["data"]
    check("team size + industry captured, not yet sales-ready",
          d["team_size"] == "40" and d["industry"] == "logistics" and d["tier"] != "sales_ready",
          f"score={d['lead_score']} tier={d['tier']}")
    check("no notifications fired before sales-ready", len(logs_for(pro_id)) == 0)

    m3 = chat(pro_id, sid, v1,
              "I'm Ada Okafor, ada.okafor@example.com, Acme Group Logistics, team of 40. We're "
              "comparing you with Intercom and I'd like to buy the Professional plan.",
              "chat-3-contact-and-ready-to-buy",
              "Contact details + ready to buy + competitor + feature question: crosses sales-ready")
    j3 = m3.json()
    meta3 = (j3.get("message") or {}).get("meta") or {}
    d = call("lead-detail-sales-ready", "Lead detail after crossing the threshold", "GET",
             f"/leads/{lead_id}", token=pro).json()["data"]
    check("lead captured (name/company/email) and sales-ready (>=86)",
          d["email"] == "ada.okafor@example.com" and d["company"] == "Acme Group Logistics"
          and d["lead_score"] >= 86 and d["tier"] == "sales_ready",
          f"score={d['lead_score']} tier={d['tier']}")
    check("lead_captured meta on the turn the email first appears",
          bool(meta3.get("lead_captured")))
    check("PII encrypted at rest (leads.email is not plaintext in the DB)",
          "ada.okafor@example.com" not in str(db_rows("select email from leads where id=:i", i=lead_id)))
    check("CRM webhook URL encrypted at rest",
          HOOK not in str(db_rows("select crm_webhook_url from tenant_sales_agent_config where tenant_id=:t", t=pro_id)))

    print("== 4. quotation (computed by code from pricing_tiers)")
    quote = meta3.get("quote")
    check("quote card present in the chat response meta", quote is not None)
    if quote:
        # Professional (11-50 users) base 150000, 40 users => no overage, 20% annual discount.
        exp_total = 150000 - 150000 * 20 // 100
        check("total = 150,000 - 20% = 120,000 computed in code, currency NGN",
              quote["total"] == exp_total and quote["currency"] == "NGN",
              f"total={quote['total']} currency={quote['currency']}")
        check("line items: tier base + annual discount only",
              [li["label"] for li in quote["line_items"]] == ["Professional", "Annual discount"])
    check("meeting picker NOT offered on this turn (no demo ask)", not meta3.get("offer_meeting"))
    qrow = db_rows("select id, total, currency, status from quotes where tenant_id=:t", t=pro_id)
    check("quote persisted and linked to the lead", len(qrow) == 1
          and db_rows("select quote_id from leads where id=:i", i=lead_id)[0]["quote_id"] == qrow[0]["id"])

    print("== 5. notifications (email stub, in-app, Slack, CRM)")
    wait_for(lambda: all(x["status"] != "pending" for x in logs_for(pro_id)) and len(logs_for(pro_id)) >= 6,
             "worker to deliver", 40)
    lg = logs_for(pro_id)
    save("notification-logs-after-sales-ready-and-quote", "notification_logs rows (db)", lg)
    ev = {(x["event"], x["channel"]): x["status"] for x in lg}
    check("sales_ready: email/in_app/slack/crm all sent",
          all(str(ev.get(("sales_ready_lead", c))).lower().endswith("sent") for c in ("email", "in_app", "slack", "crm")),
          json.dumps({k[1]: v for k, v in ev.items() if k[0] == "sales_ready_lead"}))
    check("quote_generated: email + in_app + slack sent (CRM is sales-ready only)",
          all(str(ev.get(("quote_generated", c))).lower().endswith("sent") for c in ("email", "in_app", "slack"))
          and ("quote_generated", "crm") not in ev)
    rc = received()
    crm = [x for x in rc if x["path"] == "/crm"]
    slack = [x for x in rc if x["path"] == "/slack"]
    save("crm-webhook-received", "Exact POST body the worker sent to the local CRM stand-in", crm)
    save("slack-webhook-received", "Exact Block Kit bodies the worker sent to the Slack stand-in", slack)
    check("CRM payload landed with the lead fields", len(crm) == 1
          and crm[0]["body"].get("email") == "ada.okafor@example.com"
          and crm[0]["body"].get("lead_score", 0) >= 86 and crm[0]["body"].get("company") == "Acme Group Logistics",
          str({k: crm[0]["body"].get(k) for k in ("lead_score", "status", "intent", "team_size")}) if crm else "none")
    check("CRM id written back to the lead (Lead.crm_id)",
          bool(db_rows("select crm_id from leads where id=:i", i=lead_id)[0]["crm_id"]),
          str(db_rows("select crm_id from leads where id=:i", i=lead_id)[0]["crm_id"]))
    check("Slack bodies are Block Kit with a dashboard link",
          len(slack) == 2 and all("blocks" in s["body"] for s in slack)
          and "View in dashboard" in json.dumps(slack[0]["body"]))
    nl = call("notifications-in-app", "Dashboard bell list", "GET", "/notifications", token=pro).json()
    check("in-app notifications listed for the tenant admin", nl["data"]["total"] == 2 if "data" in nl else nl.get("total") == 2,
          str(nl)[:200])
    mm = mails()
    save("mail-inbox-after-quote", "Mailpit message list (email stub)",
         [{"To": [t["Address"] for t in m["To"]], "Subject": m["Subject"],
           "Attachments": m.get("Attachments")} for m in mm])
    quote_mail = next((m for m in mm if m["Subject"] == "New quote generated"), None)
    check("rep alert emails delivered to the stub SMTP",
          any(m["Subject"] == "New sales-ready lead" for m in mm) and quote_mail is not None)
    pdf_ok = False
    if quote_mail:
        full = http.get(MAIL + f"/message/{quote_mail['ID']}").json()
        atts = full.get("Attachments") or []
        if atts:
            part = http.get(MAIL + f"/message/{quote_mail['ID']}/part/{atts[0]['PartID']}")
            (OUT / "quote-attachment.pdf").write_bytes(part.content)
            pdf_ok = part.content[:5] == b"%PDF-"
            import io as _io

            import PyPDF2

            _text = "\n".join(pg.extract_text() or "" for pg in PyPDF2.PdfReader(_io.BytesIO(part.content)).pages)
            _fonts = []
            for pg in PyPDF2.PdfReader(_io.BytesIO(part.content)).pages:
                for f in pg["/Resources"].get_object()["/Font"].get_object().values():
                    _fonts.append(str(f.get_object()["/BaseFont"]))
            check("FIX 1: quote PDF text has the naira sign (no black box) and the total",
                  "₦120,000" in _text and "■" not in _text, repr(_text.split("\n")[:8]))
            check("FIX 1: PDF embeds DejaVu Sans (not Helvetica)",
                  any("DejaVu" in f for f in _fonts), str(_fonts))
            try:
                from pypdf import PdfReader  # noqa: F401
            except Exception:
                pass
    check("quote PDF attached to the quote email and is a valid PDF", pdf_ok,
          f"{(OUT / 'quote-attachment.pdf').stat().st_size} bytes" if pdf_ok else "")

    print("== 6. meeting booking")
    m4 = chat(pro_id, sid, v1, "Great. Can we book a demo call this week?", "chat-4-demo-request",
              "Visitor asks to book: picker should be offered")
    meta4 = (m4.json().get("message") or {}).get("meta") or {}
    check("offer_meeting flag set on the demo-request turn", meta4.get("offer_meeting") is True)
    av = call("meetings-availability", "Picker slots (availability window, Africa/Lagos)", "GET",
              f"/chat/meetings/availability?conversation_id={sid}&days=3")
    slots = av.json()["data"]["slots"]
    check("availability slots generated from the window", len(slots) > 0, f"{len(slots)} slots / 3 days")
    t_slot = slots[0]["start_utc"]
    check("slots are future, on the 30-minute grid, inside 09:00-17:00 Lagos",
          all(datetime.fromisoformat(s["start_utc"]) > datetime.now(UTC) for s in slots)
          and all(9 <= (datetime.fromisoformat(s["start_utc"]) + timedelta(hours=1)).hour < 17
                  and datetime.fromisoformat(s["start_utc"]).minute in (0, 30) for s in slots))
    bk = call("meetings-book", "Visitor books the first slot", "POST", "/chat/meetings",
              body={"conversation_id": sid, "scheduled_at": t_slot, "visitor_timezone": "Africa/Lagos",
                    "use_case": "dispatch automation"})
    check("booking 200", bk.status_code == 200 and bk.json().get("success"), str(bk.json())[:160])
    dup = call("meetings-book-same-slot-same-lead", "Same lead, same slot again (double-submit)",
               "POST", "/chat/meetings", body={"conversation_id": sid, "scheduled_at": t_slot})
    check("same lead re-submitting returns the existing meeting (idempotent, one live meeting per lead)",
          dup.status_code == 200 and dup.json()["data"]["meeting"]["scheduled_at"].startswith(t_slot[:16]))
    other = call("meetings-book-other-slot-same-lead", "Same lead picks a DIFFERENT slot after booking",
                 "POST", "/chat/meetings", body={"conversation_id": sid, "scheduled_at": slots[4]["start_utc"]})
    check("a second pick does not reschedule: the original meeting is returned (200, no new row)",
          other.status_code == 200 and other.json()["data"]["meeting"]["scheduled_at"].startswith(t_slot[:16]),
          f"asked {slots[4]['start_utc']} got {other.json()['data']['meeting']['scheduled_at']}")
    check("exactly one meeting row for the lead",
          db_rows("select count(*) c from meetings where lead_id=:i", i=lead_id)[0]["c"] == 1)
    # a DIFFERENT visitor asks for the SAME slot: needs their own lead (created in step 7's
    # second conversation), so this check runs after step 7 (see below).
    past = call("meetings-book-past", "A slot in the past", "POST", "/chat/meetings",
                body={"conversation_id": sid,
                      "scheduled_at": (datetime.now(UTC) - timedelta(days=1)).isoformat()})
    check("past slot refused 409", past.status_code == 409, past.text[:160])
    av2 = call("meetings-availability-after-booking", "The booked slot (and buffer) disappear", "GET",
               f"/chat/meetings/availability?conversation_id={sid}&days=3").json()["data"]["slots"]
    check("booked slot no longer offered", t_slot not in [s["start_utc"] for s in av2])
    wait_for(lambda: any(m["Subject"] == "Your meeting is booked" for m in mails()), "visitor mail", 30)
    wait_for(lambda: len(logs_for(pro_id)) >= 11 and all(x["status"] != "pending" for x in logs_for(pro_id)), "meeting notifs", 30)
    mm = mails()
    check("visitor confirmation email sent to the lead's address",
          any(m["Subject"] == "Your meeting is booked" and m["To"][0]["Address"] == "ada.okafor@example.com" for m in mm))
    check("rep 'New meeting booked' email sent",
          any(m["Subject"] == "New meeting booked" for m in mm))
    d = call("lead-detail-final", "Lead detail with meeting + quote + CRM block", "GET",
             f"/leads/{lead_id}", token=pro).json()["data"]
    check("lead detail carries meeting, quote and crm", bool(d["meeting"]) and bool(d["quote"]),
          f"score={d['lead_score']} tier={d['tier']} crm={d['crm']}")
    check("FIX 2: lead stays sales_ready (score held) after the later demo-request turn",
          d["lead_score"] >= 86 and d["tier"] == "sales_ready",
          f"score={d['lead_score']} tier={d['tier']}")
    from urllib.parse import urlparse  # noqa: F401

    base = "http://localhost:3000"  # FRONTEND_URL in the local env
    want = f"{base}/dashboard/grow/leads/{lead_id}"
    full_mails = [http.get(MAIL + "/message/" + m["ID"]).json() for m in mails()]
    rep = [m for m in full_mails if m["Subject"] in ("New sales-ready lead", "New quote generated", "New meeting booked")
           and m["To"][0]["Address"] == "sales-rep@example.com"]
    check("FIX 3: every rep email for this lead links to its dashboard page, none to '#'",
          len(rep) >= 3 and all(f'href="{want}"' in m["HTML"] and 'href="#"' not in m["HTML"] for m in rep),
          f"{len(rep)} emails, want {want}")
    slack_links = [json.dumps(s["body"]) for s in received() if s["path"] == "/slack"]
    check("FIX 3: Slack 'View in dashboard' uses the same deep link",
          bool(slack_links) and all(want in x for x in slack_links[:3]), want)
    save("notification-logs-final", "all notification_logs rows after the full journey", logs_for(pro_id))
    n_sr = [x for x in logs_for(pro_id) if x["event"] == "sales_ready_lead"]
    check("sales_ready dispatched exactly once per channel (no duplicates)", len(n_sr) == 4, f"{len(n_sr)} rows")
    r = call("lead-rescore", "Re-score then check no duplicate notifications", "POST",
             f"/leads/{lead_id}/score", token=pro)
    check("re-scoring does not duplicate sales_ready notifications",
          len([x for x in logs_for(pro_id) if x["event"] == "sales_ready_lead"]) == 4)

    m5 = chat(pro_id, sid, v1, "Confirming: I'm ready to buy now and still comparing Intercom features.",
              "chat-5-recross-sales-ready", "Score recovers to >=86 after having dropped: crosses again")
    d5 = call("lead-detail-after-recross", "Lead after the second crossing", "GET", f"/leads/{lead_id}",
              token=pro).json()["data"]
    time.sleep(5)
    check("second crossing of the threshold did NOT re-dispatch (dedupe index holds)",
          len([x for x in logs_for(pro_id) if x["event"] == "sales_ready_lead"]) == 4
          and len([x for x in received() if x["path"] == "/crm"]) == 1,
          f"score after recross={d5['lead_score']}; /crm POSTs={len([x for x in received() if x['path']=='/crm'])}")

    print("== 7. quote with overage (60 seats on Professional, 11-50)")
    v2 = "smoke-visitor-2"
    sid2 = new_session(pro_id, v2, "chat2-session-create")
    chat(pro_id, sid2, v2, "How much does it cost? About 60 people here.", "chat2-1", "second visitor")
    chat(pro_id, sid2, v2, "We're about 60 people in logistics.", "chat2-2", "team size 60")
    m = chat(pro_id, sid2, v2,
             "I'm Bola Ade, bola.ade@example.com, team of 60 at Acme Group Logistics. Comparing "
             "with Intercom, I'd like to buy Professional.", "chat2-3-quote-with-overage",
             "60 seats on Professional: 10 overage seats at 3,000, 20% off")
    q2 = ((m.json().get("message") or {}).get("meta") or {}).get("quote")
    exp = (150000 + 10 * 3000) - (150000 + 10 * 3000) * 20 // 100
    check("overage quote: (150,000 + 10 x 3,000) - 20% = 144,000", bool(q2) and q2["total"] == exp,
          f"total={q2['total'] if q2 else None} items={[ (i['label'], i['amount']) for i in q2['line_items']] if q2 else None}")

    xdup = call("meetings-book-same-slot-other-lead", "A different visitor asks for the booked slot",
                "POST", "/chat/meetings", body={"conversation_id": sid2, "scheduled_at": t_slot})
    check("another lead cannot take a booked slot: 409 SLOT_UNAVAILABLE",
          xdup.status_code == 409 and xdup.json()["error"]["code"] == "SLOT_UNAVAILABLE", xdup.text[:160])

    print("== 8. plan gating: Lite has sales_close OFF")
    r = call("lite-config-enable-quotes", "Lite tenant tries to enable quotes + booking", "PATCH",
             "/sales/config", token=lite,
             body={"agent_enabled": True, "meeting_booking_enabled": True, "quotation_enabled": True})
    refusal = r.json()
    check("Lite cannot enable quotes/booking: refused", r.status_code in (402, 403), f"status={r.status_code} body={json.dumps(refusal)[:300]}")
    r = call("lite-config-enable-crm", "Lite tenant tries to enable CRM/Slack", "PATCH", "/sales/config",
             token=lite, body={"crm_webhook_url": f"{HOOK}/crm", "notify_channels": {"email": True, "crm": True}})
    check("Lite cannot enable CRM/Slack: refused", r.status_code in (402, 403), f"status={r.status_code}")
    # lead capture + scoring are NOT gated: enable only those, then drive a chat.
    r = call("lite-config-lead-capture-only", "Lead capture on Lite (allowed)", "PATCH", "/sales/config",
             token=lite, body={"agent_enabled": True, "lead_capture_enabled": True,
                               "notify_channels": {"email": True, "in_app": True},
                               "notification_email": "lite-rep@example.com",
                               "pricing_tiers": PRICING_TIERS, "currency": "NGN",
                               "annual_discount_pct": 20, "qualification_questions": QUALIFICATION})
    check("Lite can save lead-capture-only config", r.status_code == 200)
    # A downgraded tenant keeps stored booking/quote switches: simulate that state directly.
    db = _db()
    from sqlalchemy import select as _sel

    from app.db.models.sales_agent_config import TenantSalesAgentConfig as C

    row = db.execute(_sel(C).where(C.tenant_id == uuid.UUID(lite_id))).scalar_one()
    row.meeting_booking_enabled = True
    row.quotation_enabled = True
    row.availability = AVAIL
    db.commit()
    db.close()
    lv = "smoke-visitor-lite"
    lsid = new_session(lite_id, lv, "lite-session-create")
    chat(lite_id, lsid, lv, "How much does it cost? We're a logistics company.", "lite-chat-1", "Lite visitor")
    chat(lite_id, lsid, lv, "We're about 40 people in logistics.", "lite-chat-2", "Lite visitor")
    lm = chat(lite_id, lsid, lv,
              "I'm Ada Okafor, ada.okafor.lite@example.com, Acme Group Logistics, team of 40. "
              "Comparing with Intercom, I'd like to buy Professional.", "lite-chat-3-would-be-quote",
              "Same turn that produced a quote on Professional")
    lmeta = (lm.json().get("message") or {}).get("meta") or {}
    check("Lite: no quote card in the response", "quote" not in lmeta)
    ll0 = call("lite-lead-after-crossing", "Lite lead right after the buying turn", "GET", "/leads",
               token=lite).json()["data"]
    check("Lite lead capture + scoring work (sales-ready on the buying turn)",
          ll0["total"] == 1 and (ll0["items"][0]["lead_score"] or 0) >= 86,
          f"score={ll0['items'][0]['lead_score']} tier={ll0['items'][0]['tier']}")
    lm4 = chat(lite_id, lsid, lv, "Great. Can we book a demo call this week?", "lite-chat-4-demo", "Lite demo ask")
    check("Lite: no meeting offered", not (lm4.json().get("message") or {}).get("meta", {}).get("offer_meeting"))
    lp = [x for x in llm_requests() if x["visitor_message"].startswith("Great. Can we book") ][-1]
    check("Lite: directive tells the model scheduling is NOT available",
          "Meeting scheduling is NOT available" in lp["system_prompt"])
    lav = call("lite-meetings-availability", "Stored booking switch ON but plan lacks sales_close", "GET",
               f"/chat/meetings/availability?conversation_id={lsid}&days=3")
    check("Lite: availability returns no slots", lav.json()["data"]["slots"] == [])
    lbk = call("lite-meetings-book", "Direct booking attempt on Lite", "POST", "/chat/meetings",
               body={"conversation_id": lsid, "scheduled_at": (datetime.now(UTC) + timedelta(days=1)).isoformat()})
    check("Lite: booking refused 403 BOOKING_DISABLED",
          lbk.status_code == 403 and lbk.json()["error"]["code"] == "BOOKING_DISABLED", lbk.text[:160])
    check("Lite: no quote row created", db_rows("select count(*) c from quotes where tenant_id=:t", t=lite_id)[0]["c"] == 0)
    time.sleep(6)
    llogs = logs_for(lite_id)
    save("lite-notification-logs", "Lite tenant: email + in-app only, no slack/crm", llogs)
    check("Lite lead still reaches sales-ready and notifies by email + in-app only",
          {x["channel"] for x in llogs if x["event"] == "sales_ready_lead"} == {"email", "in_app"},
          str({x["channel"] for x in llogs}))

    print("== 9. SSRF guard on the CRM path")
    crm_posts_before = len([x for x in received() if x["path"] == "/crm"])
    from app.utils.pinned_http import UnsafeOutboundURLError, resolve_pinned_ips

    for label, url in (("loopback", f"{HOOK}/crm"), ("cloud-metadata", "http://169.254.169.254/x")):
        try:
            resolve_pinned_ips(url)
            check(f"unmodified guard blocks {label}", False, "NOT blocked")
        except UnsafeOutboundURLError as e:
            check(f"unmodified guard blocks {label}", True, str(e))
    # Live: point the Professional CRM URL at link-local and re-trigger via a fresh lead.
    call("sales-config-crm-to-metadata", "Point CRM at link-local", "PATCH", "/sales/config", token=pro,
         body={"crm_webhook_url": "http://169.254.169.254/x"})
    v3 = "smoke-visitor-3"
    sid3 = new_session(pro_id, v3, "chat3-session-create")
    chat(pro_id, sid3, v3, "How much does it cost? We run logistics.", "chat3-1", "third visitor")
    chat(pro_id, sid3, v3, "We're about 30 people in logistics.", "chat3-2", "team size 30")
    chat(pro_id, sid3, v3, "I'm Chi Eze, chi.eze@example.com, team of 30 at Acme Group Logistics. Comparing "
         "with Intercom, I'd like to buy Professional.", "chat3-3", "third lead crosses sales-ready")
    wait_for(lambda: any(x["channel"] == "crm" and x["status"] == "failed" for x in logs_for(pro_id)), "crm fail", 30)
    cf = [x for x in logs_for(pro_id) if x["channel"] == "crm"]
    save("crm-blocked-host-log", "CRM log rows after pointing at 169.254.169.254", cf)
    check("link-local CRM target: FAILED 'blocked webhook host', no POST",
          any(x["status"] == "failed" and x["error"] == "blocked webhook host" for x in cf)
          and len([x for x in received() if x["path"] == "/crm"]) == crm_posts_before)

    print("== 9b. a brand-new tenant that never changed 'who answers first'")
    hum_id, hum_email, hum = register("human", "professional")
    call("human-config-save", "Sales agent on, first_responder left at its default", "PATCH",
         "/sales/config", token=hum, body={"agent_enabled": True, "lead_capture_enabled": True,
                                           "pricing_tiers": PRICING_TIERS, "currency": "NGN"})
    hv = "smoke-visitor-human"
    hsid = new_session(hum_id, hv, "human-session-create")
    hr = chat(hum_id, hsid, hv, "How much does it cost? We run a logistics company.", "human-chat-1",
              "Buying intent on a tenant still at first_responder=human")
    hb = hr.json()
    check("default tenant: AI does not answer, visitor is escalated to a human ('Human-only mode')",
          hb.get("should_escalate") is True and hb.get("escalation_reason") == "Human-only mode"
          and (hb.get("message") or {}).get("meta", {}).get("human_first") is True,
          f"reply={hb.get('message', {}).get('content')!r}")
    check("default tenant: no lead is created from the buying-intent message",
          db_rows("select count(*) c from leads where tenant_id=:t", t=hum_id)[0]["c"] == 0)

    print("== 10. worker health")
    import os

    for tag, env in (("worker", "SMOKE_LOG_WORKER"), ("api", "SMOKE_LOG_API")):
        p = os.environ.get(env)
        raw = Path(p).read_text(errors="replace") if p and Path(p).exists() else ""
        # ENVIRONMENT=development turns on DEBUG logging and SQL echo (production runs at INFO:
        # app/core/logger.py, app/db/session.py). Judge the INFO-and-above lines, which is what
        # production emits; the DEBUG-only finding is reported separately in the SOP note.
        text = "\n".join(
            ln for ln in raw.splitlines() if "DEBUG" not in ln and "sqlalchemy.engine" not in ln
        )
        check(f"{tag} log has no lead PII or webhook URLs",
              "ada.okafor@example.com" not in text and "chi.eze@example.com" not in text
              and f"{HOOK}/crm" not in text and f"{HOOK}/slack" not in text,
              f"{len(text)} bytes scanned")
        errs = [ln for ln in text.splitlines() if "Traceback" in ln]
        check(f"{tag} log has no tracebacks", not errs, f"{len(errs)} traceback lines")

    summary = {"passed": sum(c["pass"] for c in CHECKS), "failed": [c for c in CHECKS if not c["pass"]],
               "total": len(CHECKS)}
    (OUT / "results.json").write_text(json.dumps({"summary": {"passed": summary["passed"],
                                                              "total": summary["total"]},
                                                  "checks": CHECKS}, indent=2) + "\n")
    print(f"\n{summary['passed']}/{summary['total']} checks passed")
    for c in summary["failed"]:
        print("  FAILED:", c["check"], c["detail"])
    return 0 if not summary["failed"] else 1


if __name__ == "__main__":
    sys.exit(main())
