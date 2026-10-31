# FE integration guide: quote emailed to the visitor

Every body, status and string below is copied from a live run against the running app (API, worker, Postgres, Redis, Mailpit as the SMTP stand-in). Captures: `docs/fe-integration-captures/sales-quote-email/` (README there). Addresses are fake placeholders (`alice@example.com`). Backend SOP: `docs/sop/sales_quote_visitor_email.md`.

## 1. What it is, in the FE's terms
When the sales agent shows a visitor a quote (the `meta.quote` card) and the visitor has given an email address, the backend now **emails the quote to the visitor itself** (body + PDF) and tells them so **in the text of the assistant reply**. There is **no new endpoint, no new field and no new `meta` key**: the FE only needs to render the reply text as it already does. The AI itself can no longer promise an email ("should I email it to you?" is removed from every reply server-side).

Triggers: (a) the quote is generated on a turn where the lead already has an address; (b) the visitor gives their address on a later turn after a quote was shown.

## 2. The endpoint (unchanged)
`POST /api/v1/chat/message` (public), same request and response as before. Real request used in the capture:

```json
{"message": "Hi, I'm Alice, alice@example.com. We are 58 people: how much is Growth?",
 "tenant_id": "<tenant uuid placeholder>", "user_identifier": "live-1"}
```

Real response (`200`, trimmed to the relevant fields; `01_chat_visitor_gives_email.response.json`):

```json
{
  "session_id": "b96ba9a4-0b46-40d7-b4d0-e552e25e08aa",
  "message": {
    "role": "assistant",
    "content": "Thanks! For 58 people the Growth plan fits. Your quote is below.\n\nI've sent the quote to a***@example.com. If it doesn't arrive in a few minutes, please check your spam folder.",
    "meta": {
      "lead_captured": {"name": "Alice Obi", "email": "alice@example.com"},
      "quote": {"currency": "NGN", "total": 62050,
                "line_items": [{"label": "Growth", "detail": "11–50 users", "amount": 45000},
                               {"label": "Extra seats (×8)", "detail": "₦3,500 / seat", "amount": 28000},
                               {"label": "Annual discount", "detail": "−15%", "amount": -10950}]}
    }
  },
  "should_escalate": false, "escalation_reason": null, "ticket_id": null
}
```

## 3. The reply sentences (exact strings, all captured)
The reply is the model's text with a **second paragraph (`\n\n`) added by the backend** when something email-related happened. Render `content` as given.

| Situation | Appended sentence (captured) | Capture |
|---|---|---|
| Queued for sending | `I've sent the quote to a***@example.com. If it doesn't arrive in a few minutes, please check your spam folder.` | `01_…`, `02b_…` |
| A cap stopped it | `I couldn't email the quote just now, but it's right here in our chat.` | `03_per_address_cap.json` attempt 4 |
| Quote shown, visitor has no address yet | `If you'd like a copy by email, share your email address here and I'll send it.` | `02a_…` |
| Nothing can be sent (tenant has no reply address, switch off, invalid address, quote already sent) | nothing appended, and the model's email sentence is removed | `04_no_reply_to.response.json`: `"Thanks! For 58 people the Growth plan fits. Your quote is below."` |

## 4. Field and wording traps
- **The address is masked in the reply** (`a***@example.com`: first letter + domain). Do not try to rebuild or parse it; do not show the full address next to it.
- **`meta.lead_captured.email` still carries the full address** (existing behaviour, the visitor's own session). Do not copy it into the reply UI.
- `meta.quote` is unchanged and is still the source of truth for the quote card. The reply sentence is **not** a delivery receipt: "sent" means queued; the email follows within seconds to minutes.
- The sentence is plain text produced by code. **Never match on it** to drive UI state (there is no machine-readable flag; if the FE needs one, ask for a `meta.quote_email` field).
- WhatsApp: the same reply text is forwarded; the invitation sentence is never added there.

## 5. UX consequences the FE must surface
- Do **not** add your own "we emailed you" toast or banner; the reply already says it, and it can say "couldn't".
- If the visitor mistyped their address, the masked address in the reply is their only cue; keep it visible in the transcript.
- What the visitor receives (captures `01_visitor_email.*`): From `<tenant name> via Luran <platform sender>`, a generic `Hello,` greeting (the name the visitor typed is never put in the email), subject `Your quote from <company>`, quote lines + total in the body, the PDF attached, `Reply-To` = the tenant's sales notification address. No links, images or tracking.
- Caps (per address 3/24 h across tenants, per conversation 3/24 h, per IP 10/24 h, per tenant 50/24 h): a visitor who triggers many quotes will see `I couldn't email the quote just now…`; this is normal, not an error.

## 6. Dashboard / rep side
The rep alert email (and Slack message) now lists name, company, email, phone (only when the visitor record has one), channel, total and quote lines, plus the existing "View in Dashboard" link to `/dashboard/grow/leads/<lead id>`. No API shape changed; the dashboard needs nothing.

## 7. Verification table
| Behaviour | Status |
|---|---|
| Reply with masked confirmation when the visitor gives an address with the quote | verified live (`01_chat_visitor_gives_email.response.json`) |
| Visitor email arrives with PDF, Reply-To and tenant name (Mailpit) | verified live (`01_visitor_email.eml`) |
| Invitation when no address, send after the address arrives | verified live (`02a_`, `02b_`, `02_emails_summary.json`) |
| Per-address cap: 3 delivered, 4th `couldn't email`, nothing delivered | verified live (`03_*`) |
| No reply address: nothing sent, nothing promised | verified live (`04_*`) |
| Per-conversation / per-IP / per-tenant caps, kill switch, WhatsApp, enqueue failure | **tests only** (no live run of those paths) |
| A real model's wording; a real SMTP provider and inbox; PDF in a mail client | **not exercised** (language model stood in; SMTP is Mailpit) |
| Offerings (catalog) quotes | **not wired yet** (S2 follow-up in the SOP) |
