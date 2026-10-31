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
