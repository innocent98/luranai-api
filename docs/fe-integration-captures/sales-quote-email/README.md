# Live capture: quote emailed to the visitor (2026-10-31)

Real FastAPI route, Postgres 15, Redis 7, the ARQ worker and Mailpit (SMTP stand-in); **only the language model and vector search are stood in** (`tools/live_api.py`). Addresses are fake (`example.com`, `.example`). The stand-in "model" always writes `Thanks! ... Should I email it to you?` (the sentence the production model wrote in the failed prod test); the chat replies below show the guard removing it.

Re-run: start Postgres/Redis/Mailpit on free ports, `alembic upgrade head`, `python tools/seed.py` (prints two tenant ids), `python tools/live_api.py <port>`, `arq app.workers.document_processor.WorkerSettings` (SMTP env pointed at Mailpit, `SMTP_TLS=false`), then `python tools/capture.py <with_rep_id> <without_rep_id> <out_dir>`.

| File | What |
|---|---|
| `01_chat_visitor_gives_email.response.json` | visitor gives an address in the chat: reply carries the masked confirmation |
| `01_emails_summary.json`, `01_visitor_email.*`, `01_rep_alert.*` | the visitor email (`.eml` raw, `.body.html`, `.body.txt`, the PDF) and the rep alert (the rep's HTML body, `.eml`, PDF) |
| `02a_*`, `02b_*`, `02_*` | quote first (invitation), address afterwards ("yes please, ...") -> send |
| `03_per_address_cap.*` | four sessions, one address: three delivered, fourth capped |
| `04_no_reply_to.*` | tenant with no rep address: no send, no promise |
| `notification_logs.txt` | the log rows after the run (`quote_emailed_visitor` rows carry a hashed `body_key`, never the address, in the cap key) |

The docker bridge address in the `Received:` headers is replaced by `LOCAL-DOCKER-IP`; nothing else was edited.

## `followup/` (2026-10-31): the card's email state and the masked "already sent" reply
Same stack; run `python tools/capture_followup.py <with_rep_id> <without_rep_id> followup` (the stand-in in `tools/live_api.py` now writes the production model's improvised duplicate reply, with the full address, when the visitor says "email me", and offers a quote only on pricing messages). SMTP needs `EMAILS_FROM_EMAIL`/`EMAILS_FROM_NAME` set for the worker.

| File | What |
|---|---|
| `10_card_emailed.response.json` | address known with the quote: `meta.quote.emailed/emailed_to/email_offer` |
| `11_second_yes_email_me.response.json` | second "Yes, email me": code-written, masked, `meta.quote_emailed_to`, no card |
| `12_card_offer_no_address.response.json` | quote first, no address: `email_offer: true` |
| `13_late_address.response.json`, `13_late_address.stored_card_of_earlier_message.json` | address given afterwards: `meta.quote_emailed_to` on the reply; the earlier saved card read from the database, flipped |
| `14_*`, `15_*` | tenant with no reply address: `emailed:false, email_offer:false` with and without an address |
| `16_mailbox.json` | recipients in the mailbox: one visitor email each to the two visitors (plus the rep alerts), none for the second "email me" |

