# Offerings catalog: WhatsApp photos and the quote text (stream S4b)

**What shipped:** on WhatsApp, an AI reply about 1-3 catalog items is followed by the cover photo of each item (at most 3 per reply, 6 per chat per hour, and a per-number budget), and a catalog quote goes as a plain-text message after the reply. The photo caption (`#12 Canvas sneaker · ₦12,000 · In stock`) is built **at send time from the live row**. Branch `feature/offerings-whatsapp-photos`, **draft PR #199** (stacked on S2's #197; the lead retargets it after S1 and S2 merge). No migration. Nothing merged, nothing deployed. Hardened after an independent Opus review (the "Review round" section lists what it found and what changed).

Spec: `docs/superpowers/specs/2026-10-30-offerings-catalog-design.md` sections 11, 12, 14. Plan: `docs/superpowers/plans/2026-10-30-offerings-catalog-build-plan.md` section 3 (S4b T1-T4). This is the successor note to `docs/sop/whatsapp_media_and_rating.md` for outbound images: that document covers agent images (chat attachments); this one covers catalog photos, which deliberately take a different path.

## Why

Without it a WhatsApp customer sees a text list only (the web widget gets cards; WhatsApp has none), and the catalog quote the AI built (`meta.quote`) is dropped because WhatsApp renders text only. A photo is what a buyer on WhatsApp expects; the price in its caption must never be stale; and a photo burst must not endanger the seller's number: the sidecar allows **6 sends a minute and 200 a day per number, shared by every chat's text and these photos**.

## How (decisions and what was rejected)

| Decision | Why / alternative rejected |
|---|---|
| **Chained, never racing.** `plan_followups` builds *steps* (quote text, then photos); they ride on the reply's arq job (`send_whatsapp_message(..., followups)`, sealed) and the worker queues them **only after the reply was actually sent**; the quote text job carries the photos behind it the same way | Queueing all of it at once let a quote or photo overtake a reply that was backing off after a 429/5xx. A reply that is dropped (owner took over) or retrying releases nothing |
| **Own job `send_whatsapp_offering_image(connection_id, chat_id, offering_image_id)`**, not `send_whatsapp_image` | The agent-image path needs a `message_attachments` row, and the retention job destroys the Cloudinary asset of every attachment row at expiry: reusing it would delete the catalog photo. Catalog photos never create an attachment row (live proof + a test that runs the retention job) |
| **Only a row id travels**; the sender re-reads everything | At send time: same tenant (photo and its item), item `active` and not sold out, **the catalog still on for the tenant (owner switch `tenant_offerings_settings.enabled` and the plan entitlement)**, stored URL https on `WHATSAPP_MEDIA_URL_HOSTS` (no credentials, no odd port) and the rendition URL too |
| **Caption at send time** from `facts_from_row` (S2's snapshot type): `{ref} {name} · {price_text} · {Availability}`, at most 1,024 chars | Price and availability can change between enqueue and send; reusing S2's formatter keeps wording identical to the reply |
| **Photo = `c_limit,w_1280,h_1280,q_auto:good,f_jpg`** of the optimised stored copy (`whatsapp_rendition`, built with S1's `rendition()`) | S1's `whatsapp_url` uses `w_1280` without `c_limit` (small photos are scaled up, tall portraits have no height limit); it is S1's file, so the capped copy lives here. The sidecar's own size limit is **not verified**; a 413 drops the photo |
| **Photo budgets, spent when a photo is actually sent** (`take_photo_budget`, one Lua script, three sliding windows, atomic): **6 per chat per hour; per connection `WHATSAPP_CATALOG_PHOTOS_PER_MIN` (2) and `WHATSAPP_CATALOG_PHOTO_DAILY_PCT` (15%) of `WHATSAPP_DAILY_SEND_CAP` (200) = 30 a day**; fail-closed for photos only | One catalog reply can use 5 sends (text, quote, 3 photos). Capping the photos per number leaves the minute's other sends to customer text. A dropped photo costs nothing (budgets are now spent at send, not at selection). A queue-time peek (`photo_headroom`) trims how many photo jobs are queued; it spends nothing |
| **A catalog photo is dropped, never retried** (rate cap, timeout, 5xx, refusal alike) and a redelivered job is a no-op (`claim_photo_send`, once per arq job id, 1 day) | A lost photo is cheap; a repeat is a ban risk and a second spend of the daily cap (the sidecar runs one send at a time per session, so a slow photo can outlast the backend's timeout). Image sends also get a longer client timeout (`WHATSAPP_SIDECAR_IMAGE_TIMEOUT_S`, 60 s; `linked_device.py`, which also benefits agent images) |
| **More than 3 shown items is a menu: no photos**; one cover photo each; item and photo come from our rows (the meta's `cover_image_id` is not trusted) | No photo spam for a 12-line list; the meta is a snapshot, the database is the truth |
| **Quote text only for catalog quotes** (`_is_catalog_quote`): the turn has `meta.offerings` and every quote line label is an item name of this tenant | Plan-tier quotes (software tenants) also land in `meta.quote` (`chat.py`); they must not get "The team will confirm availability and arrange payment." S2 sets no marker, so the check is the label match (a tier quote's labels are plan names). A turn that showed no card but built a real cart still qualifies |
| **Quote text rendering** (`quote_message_text`): S2's wording, sign before the symbol (`-₦5,000`), a test pins equality with `render_quote_text` for positive amounts | S2's `format_amount` puts the minus after the symbol (`₦-5,000`); that is S2's file, so the fix lives here (S2's `format_amount` is unchanged, see Follow-ups) |
| **Redelivery recovers the quote text**: if the reply enqueue failed and the event ran again, S2 suppresses the duplicate quote, so the new reply has none; `plan_followups` then takes the newest catalog quote stored on this conversation in the last 15 minutes whose text was never queued (marker `wa:catquote:<message id>`) | Otherwise the customer never gets the quote text. Redis down: the marker check says "already queued", so nothing is resent blind |
| **Follow-ups never raise** into the inbound pipeline or the text job | The reply is already queued or sent; an exception would make arq re-run it and answer the customer twice |

## What's involved

| Path | Change |
|---|---|
| `app/services/whatsapp_offering_photos.py` (new) | planning, rendering, limiter, claim, marker, send-time checks, `run_followups` |
| `app/services/whatsapp_outbound.py` | `send_offering_image` (same hard stops and sidecar error mapping as `send_image`; guard on; never retries) |
| `app/services/whatsapp_enqueue.py` | `enqueue_send_offering_image`; `enqueue_send` gains `defer_s` and sealed `followups`; `seal_steps` / `unseal_steps` |
| `app/services/whatsapp_inbound.py` | on the text and the image-message path: plan the steps and queue the reply carrying them |
| `app/workers/whatsapp.py`, `app/workers/document_processor.py` | `send_whatsapp_message` releases the chain after "sent"; arq job + registration (`max_tries=MAX_SEND_TRIES`) |
| `app/channels/linked_device.py`, `app/core/config.py` | per-call image timeout; settings `WHATSAPP_SIDECAR_IMAGE_TIMEOUT_S`, `WHATSAPP_DAILY_SEND_CAP`, `WHATSAPP_CATALOG_PHOTOS_PER_MIN`, `WHATSAPP_CATALOG_PHOTO_DAILY_PCT` |
| `tests/whatsapp/test_offering_photos.py` (new), `tests/whatsapp/conftest.py` | 60 tests; the `queue` fake records photos, `defer_s` and `followups` |
| `scripts/capture_offerings_whatsapp_photos.py`, `docs/fe-integration-captures/offerings-whatsapp/` | live capture (two modes) |

No FE contract change (no REST shape or `meta` field added). The sidecar and the chat widget are untouched.

## Review round (independent Opus review of the first version, `3ea0c1d`)

Money correctness and tenant isolation were judged sound. Fixed here, each with a test that failed first: photos shared the number's send budget with every chat's text (per-connection budgets, headroom, drop on rate cap); every `meta.quote` was sent, including plan-tier quotes with the catalog wording and `₦-5,000`; the limiter-down test never checked the text (now drives `handle_event` with Redis on a dead port); duplicate photos on a retry after a timeout (no retry, claim per job, longer timeout); ordering (chained from the reply's success); the rendition had no `c_limit`/height cap; the send-time check skipped `enabled` and the plan; the hourly budget was spent at selection even if the photo was dropped; quote text lost after a failed reply enqueue.

## Verification

* **Tests (test-first):** `tests/whatsapp/test_offering_photos.py`, 60 tests: caption format and the 1,024 cap, caption uses the price changed after queueing; rendition capped; image allowlist; cross-tenant id, forged image row, archived and sold-out after queueing, deleted photo; catalog switched off / plan without the catalog at send time; hard stops and pause started after queueing; rate cap, 413, 5xx dropped never retried, redelivery of the same job is a no-op; longer image timeout; per-connection minute budget, **two chats in one minute (the second customer's text goes out, its photo is dropped)**, daily budget share, 6 per chat per hour, a dropped photo costs nothing, limiter down; planning (1-3 items, menu, skip rules, meta cover id not trusted); **plan-tier quote shapes send nothing**, catalog quote on a no-card turn still sent; quote recovery after a retried event (and not when already queued or old); chain: the quote text carries the photos, photos staggered and trimmed to headroom, nothing follows a reply that was dropped or is backing off, a failing chain never fails the reply; inbound queues only the reply carrying the steps (text and image-message paths); **dead Redis: the `ai:` text is still queued**; **retention cannot delete a catalog photo**; negative amounts render `-₦5,000`.
* **Suites run locally (CI runs the rest):** `ruff check app/`, `black --check app/`; `tests/whatsapp`, `tests/offerings`, `tests/offerings_ai`, `tests/workers`: 1,461 passed on the pushed head.
* **Live run (2026-10-31, refreshed after the review):** real API + arq worker + Postgres 15 + Redis + fake sidecar, S2's stand-in model, stubbed Cloudinary and moderation (`scripts/capture_offerings_whatsapp_photos.py`, 9 captures). Proven live: detail reply = text then photo (rendition `c_limit,w_1280,h_1280`, live caption); a 6-item menu = text only; a sold-out item with a photo = text only; cart = reply, then the quote text, then the photo; a **second customer in the same minute** got their text and no photo, and a minute later their photo went (Redis window shows 2 photos in the minute for the number); with the connection budgets raised, a chat asking 7 times got exactly 6 photos; 0 `message_attachments` rows for the tenant.
* **NOT verified:** a real sidecar (its image-size limit, its real 429s, real pacing, order of sends), real Cloudinary renditions (the URL was never fetched), a real phone, the real model's item choices; plan-tier quotes **live** (unit-tested only: the live tenant is catalog-only); the chain's order when the reply really hits a 429 (unit-tested with the fake sidecar's forced 429; not live); a price change between enqueue and send live (unit-tested).

## Behaviour to know

* **Prices can differ across the messages of one turn.** The reply text carries the price at reply time, the photo caption the price at send time (a few seconds later, longer if the number is backed up), and the quote text the stored quote row. This is the spec's design (each is built from the live row when it is made); a price changed in that window shows as two prices in one conversation.
* **The same person can have two chats** (a LID chat and a phone chat): each has its own 6-per-hour budget, so they could get up to 12 photos an hour. The connection budgets (2 a minute, 30 a day by default) still bound the number.
* **Text is not counted in the photo budget.** The per-connection budget caps *photos*; it cannot know how many texts the number sent. With the defaults a number can still spend its minute's six sends on text, quote text and two photos; the photos are what gives way.

## Operate / roll back

* **Deploy:** API and worker **together, worker first if they cannot be simultaneous.** `send_whatsapp_message` gained a 7th argument and `enqueue_send` now always passes it, so an old worker would raise a `TypeError` on every WhatsApp text, not only catalog ones. The new `send_whatsapp_offering_image` job must be registered on the worker too.
* **Tune:** `WHATSAPP_CATALOG_PHOTOS_PER_MIN`, `WHATSAPP_CATALOG_PHOTO_DAILY_PCT`, `WHATSAPP_DAILY_SEND_CAP` (must mirror the sidecar's `DAILY_SEND_CAP`), `WHATSAPP_SIDECAR_IMAGE_TIMEOUT_S`. Setting the per-minute budget to 0 turns catalog photos off for every number without a deploy of code.
* **Roll back:** revert the PR: no migration, no data written. Redis keys `wa:catphotos:*`, `wa:catphoto:sent:*`, `wa:catquote:*` expire on their own.
* **Watch:** `grep "whatsapp catalog"` in the worker log (`photo dropped`, `limiter unavailable`, `follow-up skipped`).

## Follow-ups

* Real-phone test at the integration gate: order of reply, quote and photos; the sidecar's size limit for the 1,280 px copy; real 429 behaviour; bandwidth at the 10-number ceiling.
* Not built (owner's call): the spec's agent-transcript marker line for photos the AI sent (the AI message carries `meta.offerings`, so an agent sees what was shown).
* **For S1/S2 (not touched here):** S1's `offering_media.whatsapp_url` lacks `c_limit`/height cap (this stream does not use it); S2's `format_amount` renders negatives as `₦-5,000` (only `render_quote_text` and web text use it; a catalog quote has no negative line today).
* A repeat question about the same item re-sends its photo (bounded by the budgets); "do not repeat a photo already sent this session" would need session memory (S2's `offerings_shown`).
