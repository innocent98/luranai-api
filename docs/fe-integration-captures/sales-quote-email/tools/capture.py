"""Drive the live stack and save what really came back (chat replies, e-mails, PDFs).

Usage: python capture.py <with_rep_tenant_id> <without_rep_tenant_id> <out_dir>
Needs: the API (live_api.py), the ARQ worker and Mailpit (SMTP stand-in) running.
"""
import email
import json
import pathlib
import sys
import time
from email import policy

import httpx

API, MAIL = "http://127.0.0.1:55595", "http://127.0.0.1:55594"
with_rep, without_rep, out = sys.argv[1], sys.argv[2], pathlib.Path(sys.argv[3])
out.mkdir(parents=True, exist_ok=True)
http = httpx.Client(timeout=60)


def say(tenant, text, user, session_id=None):
    body = {"message": text, "tenant_id": tenant, "user_identifier": user}
    if session_id:
        body["session_id"] = session_id
    r = http.post(f"{API}/api/v1/chat/message", json=body)
    return r.status_code, r.json()


def save(name, data):
    (out / name).write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def mails(expect, wait=40):
    for _ in range(wait):
        msgs = http.get(f"{MAIL}/api/v1/messages").json().get("messages", [])
        if len(msgs) >= expect:
            time.sleep(1)
            return http.get(f"{MAIL}/api/v1/messages").json()["messages"]
        time.sleep(1)
    return http.get(f"{MAIL}/api/v1/messages").json().get("messages", [])


def save_mail(prefix, summary):
    raw = http.get(f"{MAIL}/api/v1/message/{summary['ID']}/raw").content
    (out / f"{prefix}.eml").write_bytes(raw)
    msg = email.message_from_bytes(raw, policy=policy.default)
    headers = {k: str(msg[k]) for k in ("From", "To", "Reply-To", "Subject") if msg[k]}
    body = msg.get_body(preferencelist=("html",))
    (out / f"{prefix}.body.html").write_text(body.get_content() if body else "")
    plain = msg.get_body(preferencelist=("plain",))
    if plain:
        (out / f"{prefix}.body.txt").write_text(plain.get_content())
    pdfs = []
    for part in msg.iter_attachments():
        data = part.get_payload(decode=True)
        (out / f"{prefix}.{part.get_filename()}").write_bytes(data)
        pdfs.append({"filename": part.get_filename(), "bytes": len(data), "magic": data[:5].decode("latin1")})
    return {"headers": headers, "attachments": pdfs}


http.delete(f"{MAIL}/api/v1/messages")

# 1. the visitor gives an address in the chat; the quote is generated in the same turn
code, first = say(with_rep, "Hi, I'm Alice, alice@example.com. We are 58 people: how much is Growth?", "live-1")
save("01_chat_visitor_gives_email.response.json", {"status": code, "body": first})
found = mails(2)
summaries = {"visitor": None, "rep": None}
for m in found:
    to = m["To"][0]["Address"]
    summaries["visitor" if to == "alice@example.com" else "rep"] = m
save("01_emails_summary.json", {
    "count": len(found),
    "visitor": save_mail("01_visitor_email", summaries["visitor"]) if summaries["visitor"] else None,
    "rep": save_mail("01_rep_alert", summaries["rep"]) if summaries["rep"] else None,
})

# 2. quote first, address afterwards (the failed prod flow: 'should I email it?' -> 'yes')
http.delete(f"{MAIL}/api/v1/messages")
code, a = say(with_rep, "How much is Growth for 58 people?", "live-2")
save("02a_chat_quote_without_email.response.json", {"status": code, "body": a})
code, b = say(with_rep, "yes please, bob@example.com", "live-2", a["session_id"])
save("02b_chat_email_given_afterwards.response.json", {"status": code, "body": b})
found = mails(2)
save("02_emails_summary.json", {"count": len(found), "to": [m["To"][0]["Address"] for m in found]})
for m in found:
    if m["To"][0]["Address"] == "bob@example.com":
        save_mail("02_visitor_email", m)

# 3. one address cannot be used to bomb a victim: 3 per 24 h across all tenants, then 'capped'
http.delete(f"{MAIL}/api/v1/messages")
caps = []
for i in range(4):
    code, r = say(with_rep, "Quote for 58 people please, victim@example.com", f"live-3-{i}")
    caps.append({"attempt": i + 1, "status": code, "reply": r["message"]["content"]})
save("03_per_address_cap.json", caps)
time.sleep(6)
victim = [m for m in http.get(f"{MAIL}/api/v1/messages").json().get("messages", [])
          if m["To"][0]["Address"] == "victim@example.com"]
save("03_per_address_cap.mailbox.json", {"emails_delivered_to_victim": len(victim)})

# 4. a tenant with nobody to reply to: nothing is sent and nothing is promised
http.delete(f"{MAIL}/api/v1/messages")
code, r = say(without_rep, "Quote for 58 people please, carol@example.com", "live-4")
save("04_no_reply_to.response.json", {"status": code, "body": r})
time.sleep(6)
save("04_no_reply_to.mailbox.json", {"to": [m["To"][0]["Address"] for m in
     http.get(f"{MAIL}/api/v1/messages").json().get("messages", [])]})
print("captured to", out)
