"""Follow-up capture: the quote card's email state and the masked 'already sent' reply.

Usage: python capture_followup.py <with_rep_tenant_id> <without_rep_tenant_id> <out_dir>
Needs the same stack as capture.py (API from live_api.py, ARQ worker, Mailpit).
"""
import json
import pathlib
import subprocess
import sys
import time

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
    return {"status": r.status_code, "body": r.json()}


def save(name, data):
    (out / name).write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def stored_card(message_id):
    """The card as persisted (what a reload shows), read straight from the database."""
    code = (
        "import json,sys;from app.db.session import SessionLocal;from app.db.models.conversation import Message;"
        "from uuid import UUID;db=SessionLocal();db.bind.echo=False;"
        "m=db.get(Message,UUID(sys.argv[1]));print(json.dumps((m.meta or {}).get('quote')))"
    )
    res = subprocess.run([sys.executable, "-c", code, message_id], capture_output=True, text=True, check=True)
    return json.loads(res.stdout.strip().splitlines()[-1])


http.delete(f"{MAIL}/api/v1/messages")

# 1. address known with the quote: queued, card says Sent to <masked>
a = say(with_rep, "Hi, I'm Alice, alice@example.com. We are 58 people: how much is Growth?", "fu-1")
save("10_card_emailed.response.json", a)

# 2. the visitor taps / types "Yes, email me" again: code-written, masked, no second email
b = say(with_rep, "Yes, email me", "fu-1", a["body"]["session_id"])
save("11_second_yes_email_me.response.json", b)

# 3. quote first with no address: the card may still offer the email
c = say(with_rep, "How much is Growth for 58 people?", "fu-2")
save("12_card_offer_no_address.response.json", c)

# 4. the address arrives later: the reply flags the earlier card; the stored card is flipped
d = say(with_rep, "yes please, bob@example.com", "fu-2", c["body"]["session_id"])
save("13_late_address.response.json", d)
save("13_late_address.stored_card_of_earlier_message.json", stored_card(c["body"]["message"]["id"]))

# 5. a tenant with nobody to reply to: no email can ever be sent, so no prompt on the card
e = say(without_rep, "How much is Growth for 58 people? carol@example.com", "fu-3")
save("14_card_no_reply_to.response.json", e)
f = say(without_rep, "How much is Growth for 58 people?", "fu-4")
save("15_card_no_reply_to_no_address.response.json", f)

time.sleep(8)
mails = http.get(f"{MAIL}/api/v1/messages").json().get("messages", [])
save("16_mailbox.json", {"to": sorted(m["To"][0]["Address"] for m in mails if "Quote" in m["Subject"] or "quote" in m["Subject"])})
print("captured to", out)
