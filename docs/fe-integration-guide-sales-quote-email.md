# FE integration guide: quote emailed to the visitor

Every body, status and string below is copied from a live run against the running app (API, worker, Postgres, Redis, Mailpit as the SMTP stand-in). Captures: `docs/fe-integration-captures/sales-quote-email/` (README there). Addresses are fake placeholders (`alice@example.com`). Backend SOP: `docs/sop/sales_quote_visitor_email.md`.

## 1. What it is, in the FE's terms
When the sales agent shows a visitor a quote (the `meta.quote` card) and the visitor has given an email address, the backend now **emails the quote to the visitor itself** (body + PDF) and tells them so **in the text of the assistant reply**. There is **no new endpoint**. The FE renders the reply text as it already does and, since the follow-up of 2026-10-31 (section 8), reads three additive fields on `meta.quote` (and one new `meta.quote_emailed_to`) to decide what the quote card's footer says. The AI itself can no longer promise an email ("should I email it to you?" is removed from every reply server-side).

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
| The visitor asks again ("Yes, email me") for a quote already emailed | the reply is the model's non-email text (if any) plus `I already emailed this quote to a***@example.com. Please check your inbox and spam folder; if it still hasn't arrived in a few minutes, ask the team to resend it.` | `followup/11_second_yes_email_me.response.json` |
| Nothing can be sent (tenant has no reply address, switch off, invalid address) | nothing appended, and the model's email sentence is removed | `04_no_reply_to.response.json`: `"Thanks! For 58 people the Growth plan fits. Your quote is below."` |

## 4. Field and wording traps
- **The address is masked in the reply** (`a***@example.com`: first letter + domain). Do not try to rebuild or parse it; do not show the full address next to it.
- **`meta.lead_captured.email` still carries the full address** (existing behaviour, the visitor's own session). Do not copy it into the reply UI.
- `meta.quote` is unchanged and is still the source of truth for the quote card. The reply sentence is **not** a delivery receipt: "sent" means queued; the email follows within seconds to minutes.
- The sentence is plain text produced by code. **Never match on it** to drive UI state: use the `meta.quote` fields in section 8.
- **The visitor's full address never appears in any reply text** (code masks it even when the model repeats it). `meta.lead_captured.email` is the one place it still travels, in the visitor's own session.
- WhatsApp: **no visitor email is ever sent and no sentence about it is added** (owner/lead decision, 2026-10-31); the model's own email promises are still removed from the reply text.

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
| Per-conversation / per-IP / per-tenant caps, kill switch, WhatsApp (never sent, no sentence), enqueue failure | **tests only** (no live run of those paths) |
| A real model's wording; a real SMTP provider and inbox; PDF in a mail client | **not exercised** (language model stood in; SMTP is Mailpit) |
| Quote card fields (`emailed`, `emailed_to`, `email_offer`) for: queued, no address yet, no reply address, address given later (earlier card flipped in the database) | verified live (`followup/10_`, `12_`, `13_`, `14_`, `15_`) |
| Second "Yes, email me": masked-only reply, no second email delivered | verified live (`followup/11_`, `16_mailbox.json`: one email each to the two visitors) |
| Card fields when a cap stopped the send; WhatsApp; switch off; the newer card taking the offer from the older one (database and open chat) | **tests only** (`tests/api/v1/test_chat_quote_visitor_email.py`, `tests/sales/test_quote_email_text.py`; chat repo `useChat.quoteEmailed.test.ts`) |
| The chat UI rendering of the footer in a real browser on mobile | **not exercised** (component tests in `luran-chat`; no browser run) |
| Offerings (catalog) quotes | **not wired yet** (S2 follow-up in the SOP) |

## 8. The quote card's email state (follow-up, 2026-10-31)
Problem it fixes: the card kept showing "Shall I email this to you as a PDF?" after the reply said the quote was sent. Three **additive** keys on `meta.quote` now say what the footer may show; every body below is a real response (`followup/`, line items trimmed to `...`).

| Key | Type | Meaning |
|---|---|---|
| `emailed` | bool | a copy of this quote is on its way to the visitor |
| `emailed_to` | string, **present only when `emailed` is true** | the **masked** address (`a***@example.com`); show it, never try to rebuild the full one |
| `email_offer` | bool | `true` only while asking for an email can still lead to one (the visitor has not given an address yet and a send is possible) |

**Rule for the card footer (implemented in `luran-chat` `QuoteCard`):**
1. `emailed` true -> show `Sent to <emailed_to>` (no prompt).
2. else `email_offer === false` -> show nothing (WhatsApp, caps, switch off, tenant without a reply address: asking would lead nowhere).
3. else (`email_offer` true **or the key is absent**, i.e. history saved before this change) -> the prompt, exactly as before.

Real bodies:

Address known with the quote (`10_card_emailed.response.json`, `message.meta.quote`):
```json
{
  "currency": "NGN",
  "line_items": ...,
  "total": 62050,
  "emailed": true,
  "emailed_to": "a***@example.com",
  "email_offer": false
}
```
Quote first, no address yet (`12_card_offer_no_address.response.json`):
```json
{
  "currency": "NGN",
  "line_items": ...,
  "total": 62050,
  "emailed": false,
  "email_offer": true
}
```
No address to reply to (tenant without a reply address; `14_card_no_reply_to.response.json`):
```json
{
  "currency": "NGN",
  "line_items": ...,
  "total": 62050,
  "emailed": false,
  "email_offer": false
}
```

**Address given after the card** (the production flow). The reply has **no card**; it carries `meta.quote_emailed_to` (`13_late_address.response.json`, `message.meta`, trimmed):
```json
{
  "quote_emailed_to": "b***@example.com"
}
```
and the earlier message's stored card is flipped by the backend (`13_late_address.stored_card_of_earlier_message.json`, so a reload shows it too):
```json
{
  "currency": "NGN",
  "line_items": ...,
  "total": 62050,
  "emailed": true,
  "email_offer": false,
  "emailed_to": "b***@example.com"
}
```
**FE instruction:** when a reply arrives with `meta.quote_emailed_to`, set the latest quote card in the open chat to emailed (`luran-chat` `markQuoteEmailed`). When a reply carries a **new** card, take `email_offer` off every older card (`closeEarlierQuoteOffers`; the backend saves the same change): only the newest card may offer the email.

**Second "Yes, email me"** for a quote that was already emailed (`11_second_yes_email_me.response.json`, `message.content` and `message.meta`; no new card, no second email):
```json
{
  "content": "I already emailed this quote to a***@example.com. Please check your inbox and spam folder; if it still hasn't arrived in a few minutes, ask the team to resend it.",
  "meta": {
    "chunks_used": 0,
    "avg_similarity": 0.0,
    "retrieval_hit": false,
    "escalation_action": "no_escalation",
    "response_quality": {
      "is_substantial": true,
      "has_uncertainty": false,
      "word_count": 26,
      "char_count": 160
    },
    "token_breakdown": {
      "prompt_tokens": 212,
      "completion_tokens": -162,
      "total_tokens": 50
    },
    "quote_emailed_to": "a***@example.com"
  }
}
```
The next step offered is "ask the team to resend it" on purpose: letting the visitor type a different address to resend to would be a way to point the platform at any inbox, which the per-address and per-conversation caps exist to limit.

### Field-nesting and null traps (card fields)
- `emailed_to` is **absent**, not `null`, when nothing was sent. `email_offer` and `emailed` are present on every card created after this change and **absent on older history**: treat absence as "as before".
- `meta.quote_emailed_to` sits on the **message** meta (next to `quote`/`lead_captured`), not inside `meta.quote`, and only on replies that did not create a card.
- Cards for Offerings (catalog) quotes do not carry these keys yet (S2): they render as before.
