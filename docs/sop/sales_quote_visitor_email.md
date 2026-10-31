# SOP: quote emailed to the visitor + richer rep alert (2026-10-31)

## What shipped
Branch `feature/sales-quote-visitor-email` (PR to `develop`, number in the PR; head SHA in the PR). Three things from the owner's prod test, where the AI showed a quote and asked "should I email it to you?", the visitor said yes, and nothing arrived (no code path emailed a visitor), while the rep email only showed a name:

| | Change |
|---|---|
| A | **Rep alert** (email, Slack, in-app) for quote, lead and meeting alerts now carries the visitor's email, phone (when the visitor record has one), name, company, conversation channel, the total with the currency symbol (same formatter as the PDF), the quote lines (for lead/meeting alerts: the lines of the quote the lead asked for) and the dashboard deep link. |
| B | **Visitor copy of the quote**: when a quote exists for a lead with a valid email, an ARQ job (`send_visitor_quote_email`) emails the visitor the quote lines + total in the body and the PDF attached, from the tenant's display name, with `Reply-To` = the address the rep alerts go to. Logged in `notification_logs` under a new event `quote_emailed_visitor`. The reply says what happened in code-written words with a masked address. |
| C | **Guard**: the model can no longer promise an email. A rule in the sales prompt, and a code filter on every reply, remove any "I'll email it / should I email it to you?" sentence; only code adds the one true sentence. |

## Why
No code path ever emailed a visitor a quote; the model improvised the offer (nothing in `app/sales/*` or `rag_service.py` asked it to) and the reply text is persisted before the quote exists, so the model could not know whether a send would happen. The rep alert built its fields from `_entity_fields`, which only knew name/company/total.

## How (decisions and why)
| Decision | Choice | Alternatives rejected |
|---|---|---|
| Send automatically or on an explicit "yes"? | **Automatic**: the quote is emailed as soon as a quote exists and the visitor has given an address (this turn, or when the address arrives after the quote). | A structured `email_quote_consent` field in `SALES_RESPONSE_SCHEMA`: needs multi-turn state (find the quote, carry the "yes"), depends on the model setting a flag reliably (the very thing that failed), touches `chat.py` and the schema that Offerings (S2) is rewriting. Auto-send is deterministic and keeps the wording in code. |
| Who words the reply | Code only (`app/sales/quote_email.py`): `I've sent the quote to a***@example.com. If it doesn't arrive in a few minutes, please check your spam folder.` / `I couldn't email the quote just now, but it's right here in our chat.` / `If you'd like a copy by email, share your email address here and I'll send it.` | Letting the model confirm: it cannot know. |
| Model promises | Prompt rule 12 (cannot send, never offer) **plus** `strip_email_promises` on every reply path (sales and plain), because a prompt is a request, not a guarantee. A sentence that also asked for the address becomes `What's the best email address to reach you on?` so contact capture keeps working. | Prompt only. |
| Dedupe / idempotency | Log row `(tenant, quote_emailed_visitor, email, quote id, address)` under the existing unique index; one visitor email per quote; ARQ job id `send_visitor_quote_email:<log id>`; the worker locks the log row (`FOR UPDATE`) and exits if `sent`; an identical quote (same lines/total) the visitor already received is not sent again (the model offers the same quote on several turns). | |
| Reply-to | `NotificationService._rep_recipient`: the same address the rep alerts use (`notification_email`, else the first tenant admin). No reply-to = no send = no promise. | Sending without a reply-to (visitor cannot answer the rep). |
| Visitor email is not tied to the tenant's `notify_channels.email` (that toggle is the rep's alert); the kill switch is a platform env setting instead. | | A per-tenant opt-out column (needs a migration, a dashboard control; follow-up if a tenant asks). |

## Consent and abuse handling
The visitor typed the address into a public chat, so someone can type another person's address. The email is a one-off transactional reply to a request made in that chat: nothing beyond the quote (no marketing, no links, no images or tracking pixel, no unsubscribe machinery), the body says why they got it ("this address was entered in a chat with <company>; if that wasn't you, you can ignore this email"), and these caps apply in this order (first failure wins; the visitor sees "couldn't email the quote just now", never which cap):

| Cap | Default | Where | Counted by | Tune |
|---|---|---|---|---|
| Kill switch | on | `QUOTE_VISITOR_EMAIL_ENABLED` | n/a | env, restart |
| Valid address (`email_validator`, no DNS) and a reply-to exists | n/a | `quote_email_service._valid_address`, `_reply_to` | | |
| One visitor email per quote (and per identical quote) | 1 | `email_quote_to_visitor` | `notification_logs` | code |
| Per tenant per rolling 24 h | 50 | `QUOTE_VISITOR_EMAIL_TENANT_DAILY_CAP` | `notification_logs` (pending+sent) | env, live |
| Per recipient address per rolling 24 h, **all tenants** | 3 | `QUOTE_VISITOR_EMAIL_RECIPIENT_CAP_24H` | `notification_logs.body` = sha256 of the lowercased address with the `+tag` removed (no plaintext in the key; `victim+1@` and `victim+2@` are one inbox) | env, live |
| Per conversation per 24 h | 3 | `QUOTE_VISITOR_EMAIL_SESSION_CAP_24H` | Redis limiter `http_quote_email_session` keyed by conversation id | env, restart |
| Per client IP per 24 h | 10 | `QUOTE_VISITOR_EMAIL_IP_CAP_24H` | Redis limiter `http_quote_email_ip` keyed by IP (public `/chat/message` only; WhatsApp and the customer-token route have no IP and skip this one) | env, restart |

The tenant and per-address caps count database rows, so a Redis flush or outage cannot lift them (the Redis limiters fail open per `RATE_LIMIT_FAIL_OPEN`, so these two are the backstop). Failed sends do not count. Known limits: dots in Gmail local parts and other aliasing are not folded together; concurrent requests can overshoot a cap by a few; there is no cross-tenant suppression list ("never email me"): a victim can be emailed up to 3 times in 24 h by an attacker rotating tenants' chats and IPs. If abuse appears, lower `QUOTE_VISITOR_EMAIL_RECIPIENT_CAP_24H` or set the kill switch.

## What's involved
| Path | Change |
|---|---|
| `app/sales/quote_email.py` (new) | `mask_email`, `strip_email_promises`, `SentOutcome`, `finalize_reply` (pure, tested) |
| `app/services/quote_email_service.py` (new) | `email_quote_to_visitor` (all checks + log + enqueue), `outcome_for_turn`, `finalize_turn_reply` (the one call `chat.py` makes), `can_email_quote`, `recipient_key` |
| `app/workers/sales_notifications.py` | `send_visitor_quote_email` job + template; `_entity_fields`/`_entity_context`/`_lead_contact`/`_quote_items` (rep alert fields), `_slack_body`/`_slack_escape` (Slack gets the details, `& < >` escaped so a name cannot `@channel`) |
| `app/workers/document_processor.py` | job registered in `WorkerSettings.functions` |
| `app/templates/emails/sales_quote_visitor.html` (new), `sales_rep_alert.html` | plain visitor email (no `base.html`, no links/images); rep alert rows |
| `app/services/email_service.py` | optional `reply_to`, `from_name`, `text_content` (attachments variant); header values stripped of control characters; nothing changes for callers that omit them |
| `app/services/quote_pdf_service.py` | public alias `format_amount` of the PDF's money formatter |
| `app/db/models/notification_log.py` + `alembic/versions/qv10_quote_visitor_email.py` | `NotifEvent.QUOTE_EMAILED_VISITOR`; revision `qv10quotevisitoremail` (`ALTER TYPE notif_event ADD VALUE IF NOT EXISTS`; downgrade is a no-op, Postgres cannot drop an enum value) |
| `app/utils/http_rate_limiter.py` | two limiters, `request_client_ip` contextvar + `bind_client_ip` dependency |
| `app/core/config.py` | five `QUOTE_VISITOR_EMAIL_*` settings |
| `app/sales/intent.py` | prompt rule 12 |
| `app/api/v1/endpoints/chat.py` (**kept minimal for Offerings S2**) | 1 import name, `Depends(bind_client_ip)` on `/message`, `_new_quote = None` + `_new_quote = _created_quote`, and one call `ai_response_text = await finalize_turn_reply(...)` right before the assistant message is saved (~13 lines in two places) |

Offerings (S2) quotes: they are ordinary `Quote` rows, so the helper works for them, but S2's own quote path (`app/offerings/orchestrate.py::issue_quote`, which calls `QuoteService.create_quote` then `_notify(... QUOTE_GENERATED ...)`) does not go through `chat.py`'s quote block. **Lines for the lead to hand to S2** (after `created = QuoteService(db).create_quote(...)` and the `_notify`): `outcome = await email_quote_to_visitor(db, created, client_ip=request_client_ip.get())` and map it into the `QuoteOutcome.note` S2 already adds to the reply (`SentOutcome.note` is the sentence; imports `from app.services.quote_email_service import email_quote_to_visitor` and `from app.utils.http_rate_limiter import request_client_ip`). S2's reply path must also keep calling `finalize_turn_reply` (or at least `strip_email_promises`) so the guard covers catalog replies. Until then S2's quotes are not emailed to the visitor and the guard still strips promises in the shared reply path.

## Verification
- **Tests first (each seen red for the right reason, then green)**: `tests/sales/test_quote_email_text.py` (mask; 12 promise phrasings removed, 6 ordinary sentences kept; the address ask becomes a plain request), `tests/sales/test_quote_visitor_email.py` (queued/logged/enqueued; once per quote and per identical quote; invalid/absent address; kill switch; no reply-to; tenant cap; per-address cap across tenants incl. `+tag`; old and failed rows do not count; session and IP limiters (keys, no address in the Redis key, missing IP skipped); enqueue failure -> log failed, no promise), `tests/workers/test_visitor_quote_email_worker.py` (subject, Reply-To, From name, PDF attached and still sent without it, plain body with no `<img>`/`href`, escaping, header injection, idempotent, failed -> retry flips to sent, registered with the worker), `tests/workers/test_rep_alert_rich.py` (fields, ₦ formatting, escaping for email and Slack, email still sent when the PDF fails, absent fields not printed as `None`), `tests/services/test_email_reply_to.py`, `tests/sales/test_directive_email_rule.py`, and **`tests/api/v1/test_chat_quote_visitor_email.py`: the C guard end to end** (switch off, no reply-to, cap hit, visitor without an email, and the plain non-sales path: a reply never promises an email when none will be sent; plus the happy path, the "email arrives after the quote" path, and no second announcement).
- **Local checks**: `ruff check app/` and `black --check app/` clean (project toolchain, ruff 0.8.6, black 24.10.0); `tests/api/v1` 424 passed / 3 skipped; full `tests/` 3529 passed / 33 skipped (shared code: `EmailService`, `chat.py`); migration: one head (`qv10quotevisitoremail`), `upgrade head` -> `downgrade -1` -> `upgrade head` on a scratch Postgres 15, enum value present.
- **Live run** against the running app (real FastAPI route, Postgres 15, Redis 7, the ARQ worker, Mailpit as the SMTP stand-in on a free port; only the language model and vector search are stood in, in `docs/fe-integration-captures/sales-quote-email/tools/live_api.py`; fake addresses on example.com only): captured in `docs/fe-integration-captures/sales-quote-email/` (README there): visitor gives an address -> chat reply + rep email + visitor email (headers, rendered body, PDF); quote first, address afterwards -> invitation, then the send; four requests for one address -> three delivered, the fourth `couldn't email` and nothing delivered; a tenant with no rep address -> nothing sent, nothing promised.
- **Not proven**: a real model's wording (the stand-in always writes "Should I email it to you?", which the guard removes; a real model may phrase an offer the regex misses, so the prompt rule is the first line and the filter the second); a real SMTP provider, a real inbox, spam placement, the PDF opened in a mail client; WhatsApp end to end (the guard and send run on the shared path; the invitation is deliberately not shown there); the Offerings quote path (S2, not merged).

## Operate / roll back
- Deploy: migration `qv10quotevisitoremail` (additive, `ADD VALUE IF NOT EXISTS`, no pre-deploy dump needed). Nothing else is required: `FRONTEND_URL` already drives the rep deep link, the SMTP settings are the ones every email uses. Optional env (defaults above): the five `QUOTE_VISITOR_EMAIL_*`.
- Switch it off without a deploy of code: `QUOTE_VISITOR_EMAIL_ENABLED=false` and restart API + worker. The guard still removes the model's email promises, and no invitation or confirmation is shown. Queued jobs already in Redis still send.
- Roll back: revert the PR. The enum value stays (harmless); rows with the event stay in `notification_logs`.
- Watch: `notification_logs` where `event='quote_emailed_visitor'` and `status='failed'` (SMTP trouble), and counts of `couldn't email` replies (caps firing).

## Follow-ups
- Offerings (S2) quote path: the two lines above, and the guard on its reply path.
- Real-model check on staging that the strip filter covers the model's real phrasings (add misses to `tests/sales/test_quote_email_text.py`).
- A per-tenant opt-out and a cross-tenant "do not email this address" suppression list if abuse or complaints appear.
- In-app notification body for quotes still shows total and company only (not the visitor's name or email, which would put PII in `notification_logs.body`).
- "Wanted items" on lead/meeting alerts come from the lead's quote; catalog (`LeadOfferingInterest`) items will appear once S1/S2 merge.
- `notification_logs.recipient` stores the visitor's address in plaintext (the same as the existing meeting-confirmation rows, and it is deleted with the tenant); the per-address cap key is a hash. Encrypt or hash the column platform-wide if that is ever required.
- `lead_captured.email` in the chat response `meta` (existing, the visitor's own address in their own session) is unchanged; only the reply text is masked.
