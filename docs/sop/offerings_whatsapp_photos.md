# Offerings catalog: WhatsApp photos and the quote text (stream S4b)

**What shipped:** on WhatsApp, an AI reply that is about 1-3 catalog items is followed by the cover photo of each item (at most 3 per reply, at most 6 per chat per hour), and a catalog quote goes as a second plain-text message after the reply. The photo caption (`#12 Canvas sneaker · ₦12,000 · In stock`) is built **at send time from the live row**, so a price or stock change made after the reply was queued is the one the customer sees. Branch `feature/offerings-whatsapp-photos`, **draft PR** stacked on S2's `feature/offerings-ai-grounding` (#197; the lead retargets it after S1 and S2 merge). No migration. Nothing merged, nothing deployed.

Spec: `docs/superpowers/specs/2026-10-30-offerings-catalog-design.md` sections 11, 12, 14. Plan: `docs/superpowers/plans/2026-10-30-offerings-catalog-build-plan.md` section 3 (S4b T1-T4). This is the successor note to `docs/sop/whatsapp_media_and_rating.md` for outbound images: that document covers agent images (chat attachments); this one covers catalog photos, which deliberately take a different path.

## Why

Without it a WhatsApp customer sees a text list only (the web widget gets cards; WhatsApp has none), and the catalog quote the AI built (`meta.quote`) is silently dropped because WhatsApp renders text only. A photo of the item is what a buyer on WhatsApp expects; the price in its caption must never be stale, and a photo burst must not endanger the seller's number.

## How (decisions and what was rejected)

| Decision | Why / alternative rejected |
|---|---|
| **Own job `send_whatsapp_offering_image(connection_id, chat_id, offering_image_id)`**, not `send_whatsapp_image` | The agent-image path needs a `message_attachments` row, and the daily retention job destroys the Cloudinary asset of every attachment row at expiry: reusing it would delete the catalog photo. Catalog photos never create an attachment row (proven live and by a test that runs the retention job) |
| **Only a row id travels in the job**; the sender re-reads everything | Nothing from the model, a frame or the job can become a URL, caption or price. At send time: same tenant (photo row and its item), item `active`, not sold out, stored URL https on `WHATSAPP_MEDIA_URL_HOSTS` (no credentials, no odd port), and the WhatsApp rendition URL must also pass |
| **Caption built at send time** from `facts_from_row` (S2's snapshot type): `{ref} {name} · {price_text} · {Availability}`, at most 1,024 chars | Price and availability can change between enqueue and send (sidecar pacing, retries). Reusing S2's formatter keeps `price from ₦5,000` wording identical to the reply |
| **Photo = `whatsapp_url()`** (1,280 px, `q_auto:good`, JPEG) of the optimised stored copy | S1's rendition helper; same host, so it passes the allowlist; at most ~1 MB. The sidecar's own size limit for a catalog send is **not verified** (needs the real sidecar) |
| **Selection** (`app/services/whatsapp_offering_photos.py`): only when the reply shows 1-3 items; one cover photo each; item and photo are re-read from our rows (the meta's `cover_image_id` is not trusted); archived, sold-out, other-tenant and photo-less items are skipped | A 12-line menu must not become photo spam; the meta is a snapshot, the database is the truth |
| **Limits: 3 photos per reply, 6 per chat per hour** | Owner decision. Each photo is one send against the sidecar's daily cap and pacing |
| **Limiter: Redis sorted-set sliding window, one Lua script (atomic), partial grants, fail-closed for photos only** | A fixed window would allow 12 photos across a window edge. If Redis is down the photos are skipped and the text still goes. Not the shared `RedisRateLimiter` (it blocks until a slot frees: wrong for "skip") |
| **Text never waits on photos**; photos are deferred 2 s (+2 s each), the quote text 1 s | The reply is enqueued first; deferral keeps the follow-ups behind it when the worker has several free slots. Real ordering through the real sidecar is **not verified** |
| **Follow-ups never raise into the inbound pipeline** | The reply text is already queued; an exception would make arq retry the whole event and answer the customer twice. A photo that cannot be queued is logged and dropped |
| **Quote text** = S2's `render_quote_text(meta.quote)`, queued as `quote:{message_id}` (idempotent), `guard=True` (pause / AI-off re-checked at send) | Every figure comes from the stored quote row; nothing is computed here |
| Not built: the "transcript marker line for human agents" of spec section 11 | The AI reply text names the items and the stored message carries `meta.offerings`, so an agent sees what was shown; a synthetic transcript row would need a message role and fan-out decisions. **Decision for the lead** |

## What's involved

| Path | Change |
|---|---|
| `app/services/whatsapp_offering_photos.py` (new) | selection, caption, Lua limiter, `live_catalog_photo` (shared by selection and send), `enqueue_catalog_followups` |
| `app/services/whatsapp_outbound.py` | `send_offering_image` (same hard stops, `guard=True`, retry policy and sidecar error mapping as `send_image`) |
| `app/services/whatsapp_enqueue.py` | `enqueue_send_offering_image`; `enqueue_send` gains optional `defer_s` |
| `app/services/whatsapp_inbound.py` | one call after the AI reply is enqueued, on the text path and the image-message path |
| `app/workers/whatsapp.py`, `app/workers/document_processor.py` | arq job + registration with `max_tries=MAX_SEND_TRIES` (the only edit outside `whatsapp_*`: the worker list lives there) |
| `tests/whatsapp/test_offering_photos.py` (new), `tests/whatsapp/conftest.py` | 35 tests; the `queue` fake records catalog photos and `defer_s` |
| `scripts/capture_offerings_whatsapp_photos.py`, `docs/fe-integration-captures/offerings-whatsapp/` | live capture (6 files) |

No FE contract changes (no REST shape, no `meta` field added). The sidecar and the chat widget are untouched.

## Verification

* **Tests (test-first; written red, then green):** `tests/whatsapp/test_offering_photos.py` covers caption format and the 1,024 cap; caption uses the price changed after queueing; sends the 1,280 px rendition; image allowlist (http, off-host, odd port, credentials); cross-tenant photo id, a forged image row claiming this tenant on another tenant's item, archived and sold-out after queueing, deleted photo; hard stops (not connected, pause started after queueing); sidecar 413 permanent, 5xx retry, give-up; arq job maps `RetrySend` to `Retry` and is registered; selection (1-3 items, 4+ none, photo-less skipped, archived / sold-out / other-tenant skipped, cover id in the meta not trusted); 6 per chat per hour (third reply loses its photos), partial grant, per chat, limiter down means no photos; inbound ordering (text before photos), a photo that cannot be queued keeps the text and does not raise; quote text second after the reply, absent without a quote, failing enqueue keeps the reply; image-message path; **retention job cannot delete a catalog photo (no attachment row exists, destroy never called)**.
* **Suites run locally (CI runs the rest):** `ruff check app/`, `black --check app/` (the CI commands); `tests/whatsapp`, `tests/offerings`, `tests/offerings_ai`, `tests/workers`: 1,193 tests, green on the pushed head.
* **Live run (2026-10-31):** real API + arq worker + Postgres 15 + Redis + fake sidecar, with the model replaced by S2's deterministic stand-in and Cloudinary/moderation stubbed (`scripts/capture_offerings_whatsapp_photos.py`; 6 captures). Proven live: detail reply = text then photo with the live caption and the `w_1280,q_auto:good,f_jpg` URL; a 6-item menu = text only; a sold-out item that has a photo = text only; cart = reply, then the quote text, then the photo; a second chat asking about items with photos 7 times received exactly 6 photos and 6 entries in the Redis window (the 7th reply kept its text); the tenant has 0 `message_attachments` rows while the sidecar received 8 photos, catalog rows intact.
* **NOT verified:** a real sidecar (its own image-size limit for a catalog send, real pacing and send order between the reply, quote text and photos), real Cloudinary (the rendition URL was never fetched; the optimised copy's real size), a real phone, the real model's choice of items, a price change landing between enqueue and send live (covered by a unit test only), voice.

## Operate / roll back

* **Deploy:** backend and worker together (new arq job `send_whatsapp_offering_image`). If the API is deployed first, the enqueue fails and is logged and dropped (text unaffected); if the worker is older than the API, queued photo jobs wait in Redis until it is updated.
* **Switch off:** there is no per-feature flag. Photos stop when the tenant has no photos, the item is archived or sold out, or the tenant's `tenant_offerings_settings.enabled` is false (S2's lookup reads that flag, so no catalog items are shown; not re-tested here). Roll back by reverting the PR: no migration, no data written.
* **Watch:** `grep "whatsapp catalog"` in the worker log (`photo dropped`, `limiter unavailable`, `follow-up skipped`). Redis keys `wa:catphotos:<chat uuid>` (no phone number in the key) expire an hour after the last photo.

## Follow-ups

* Real-phone test at the integration gate: ordering of reply, quote and photos; the sidecar's size limit for the 1,280 px copy; bandwidth at the 10-number ceiling (plan risk 4).
* Decide on the human-agent transcript marker line (not built, see table).
* A repeat question about the same item re-sends its photo (bounded by the 6 per hour cap). A "do not repeat a photo already sent in this session" rule would need session memory (S2's `offerings_shown`): a cheap v1.5 addition if the pilot shows repeats.
