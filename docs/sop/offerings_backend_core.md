# SOP: Offerings catalog, backend core (stream S1), 2026-10-31

## What shipped
Branch `feature/offerings-backend-core` -> `develop`, PR [#195](https://github.com/innocent98/luranai-api/pull/195) (opened as a draft at T1, **not merged**; the lead merges after the owner's "merge #195"). Production untouched. This is stream S1 of `docs/superpowers/plans/2026-10-30-offerings-catalog-build-plan.md` against the approved spec `docs/superpowers/specs/2026-10-30-offerings-catalog-design.md`: the tables, the plan limits, the REST API the dashboard builds on, photos, bulk import, and the daily-upkeep endpoints. The AI path (grounding, guard, cart, quote) is stream S2 and is **not** here.

| Task | What | Commit |
|---|---|---|
| T1 | Six models, migration `of10offerings`, plan columns and values, `OFFERING_REQUEST` | `bd1ddc6` |
| T2 | Entitlement meter (`offerings`), downgrade guard (items, photos per item) | `3786c4b` |
| T3 | `OfferingError` + handler, CRUD, `#seq`, cap under a row lock, lapsed write gate | `5c658e7` |
| T4 | Photos: validate, moderate, optimise on ingest, cap, order, delete, orphan sweep, tenant purge | `385eb2d` |
| T5 | Import: CSV and `lines`, preview, atomic apply, upsert | `53a7111` |
| T6 | Confirm-today, settings, Not-found list, 90-day sweep | `4f8bbef` |
| T7 | Lead detail `interests[]` | `55b0d13` |
| T8 | Live run, captures, FE guide part 1, this SOP, checklist | this commit |

FE contract: `docs/fe-integration-guide-offerings.md` (part 1), captures `docs/fe-integration-captures/offerings/` (script `scripts/capture_offerings.py`).

## Why
Luran answered "how much / is it in stock" from knowledge-base text that can be days old, and a wrong price quoted on WhatsApp becomes a dispute the business must honour. The owner approved a live catalog read from Postgres on every channel (spec section 1). S1 is the part that stores and maintains it; it was built first because S2 (AI), S3 (dashboard) and S4b (WhatsApp photos) all depend on the tables and the API shapes.

## How (decisions and what each costs if wrong)
| Decision | Why / rejected alternative | Cost if wrong |
|---|---|---|
| **Enum columns are `VARCHAR(20)` with named `CHECK` constraints**, not native Postgres enums (`native_enum=False`) | Adding a value later is a constraint swap, not `ALTER TYPE` (which cannot run in every transaction context and cannot drop a value). The suite also builds every table on SQLite. | A new value needs a migration that swaps the constraint. |
| **`UNIQUE (offering_id, position)` is not `DEFERRABLE`** (spec said deferrable) | The suite builds all tables on SQLite too, which cannot declare it. Reorder and delete move positions in two steps (park every row at `position + 100000`, then place each) inside one transaction under a row lock. | None functionally; the two-step is slightly more SQL. |
| **`#seq` and the plan cap are decided under a `FOR UPDATE` lock on `tenant_offerings_settings`** | One lock gives both a collision-free number and a cap that concurrent creates cannot overshoot. `tests/offerings/test_concurrency.py` runs 10 real threads against committed rows and **fails if the lock is removed** (checked by mutation: 2 of 5 free slots filled instead of 5). | A missing lock would let two creates take the same `#seq` (unique violation, 500) or exceed the cap. |
| **The settings row is created lazily; `GET /settings` never writes** | A read endpoint must not write; defaults come from the sales agent currency, else NGN. | None. |
| **`DELETE` archives; `?hard=true` deletes** (spec table) | Archive is the safe default (archived items do not count toward the cap and the AI cannot see them). A hard delete destroys the photo assets (best-effort) and its `#seq` is never reused. | A hard delete cannot be undone. The dashboard must confirm. |
| **Plan cap counts active items only; restoring from archive and import reactivation count** | Spec: archived items do not count. Existing items stay editable when a tenant is over its cap; only creation is blocked. | A tenant over its cap after a plan change can keep editing but not add. |
| **List `meta`** is `{next_cursor, total, page_size, limit, used}` (spec sketched `{next_cursor, total, limit, used}`) | `limit` stays the plan cap (the meter), so the page size needed its own key, `page_size`. Cursor = the last `seq` on the page; newest first. | FE could confuse `limit` and `page_size`; the guide has a trap note. |
| **Validation errors are flat** `{code:"validation", errors:[{field, code}]}`, no per-field message text | The FE owns copy. Business rules are checked in the service so every violation comes back together. A wrong *type* still gets the framework's own `detail` 422 (documented trap). | Two 422 shapes to handle. |
| **Photo flow: validate (JPEG/PNG/WEBP, 5 MB, real-format, 9,000 px) -> moderate (fail-closed) -> Cloudinary upload (1,600 px, `quality:auto`, JPEG, `strip_profile` requested) -> lock and re-check cap -> insert** | Cheapest checks first; the cap is checked before any moderation or upload cost, then **re-checked under the item lock** and the just-uploaded asset is destroyed if a concurrent upload took the last slot. A result over 800 KB is re-uploaded once over the same asset at quality 60. | Real-Cloudinary byte sizes are unverified (spec marked this unknown). |
| **Renditions are URL transformations of the one stored asset** (`thumb_url` 400 px, `gallery_url` 1,000 px, `whatsapp_url` 1,280 px good quality) | Nothing stored twice; all on the allow-listed host. `whatsapp_url` is for stream S4b. | None. |
| **New setting `CLOUDINARY_OFFERINGS_FOLDER` (default `luran/offerings`), folder `<CLOUDINARY_OFFERINGS_FOLDER>/<tenant_id>`** (spec said `{CLOUDINARY_FOLDER}/offerings/{tenant_id}`) | `CLOUDINARY_FOLDER` defaults to `luran/widget_logos`, so the spec's path would nest catalog photos under the logos folder. | A deployment that wants a different root sets the variable. |
| **New upload limiter: 200 photo uploads per hour per IP** | The existing public chat limiter is 10 per hour, far too tight for an owner filling a catalog (about 60 to 80 photos in a sitting). | A shared office IP could hit 200; raise it in `http_rate_limiter.py`. |
| **Import: `mode=upsert` matches `external_ref` first (any status; archived is reactivated and counts toward the cap), else active name case-insensitively; blank cells and absent columns leave stored values unchanged; one transaction, any bad row refuses all unless `skip_errors`** (the spec left the match rule to the builder) | Safest for the owner pasting partial lists. Rejected: blank cell clears the field (a partial paste would wipe descriptions); name matching across archived items (would resurrect old items by accident). | Someone who wants a blank cell to clear a field cannot do that through import; they edit the item. |
| **Import atomic failure is `422 import_has_errors`** (`errors[]`, `summary`) and an over-cap import is `409 offering_limit` with `can_add` | Spec said "atomic" and "reports how many fit" without bodies. | none |
| **The feature string `Offerings: up to N items` sits right after the WhatsApp line** (appended when a hand-edited list has no WhatsApp line); inserted and removed by exact match | Same element-level rule as wa30/cl10: never rewrite a list a platform admin edited. | A hand-edited plan keeps its list; fix it in the platform editor. |
| **The tenant purge** deletes the catalog tables explicitly and destroys photo assets best-effort | The tenant row is kept as a PII-free shell, so nothing cascades from it. The purge's completeness guard (every tenant-scoped table must be seeded) was extended with the six tables. | An asset the destroy missed is reaped by the orphan sweep. |
| **Downgrade guard** blocks when active items exceed the target cap or any item holds more photos than the target allows (`offerings`, `offering_images`) | Spec: blocked (`data_exceeds_limits`), never silently trimmed. | None. |

## What's involved
**New:** `app/db/models/offering.py`; `alembic/versions/of10_offerings.py`; `app/api/v1/endpoints/offerings.py`; `app/schemas/offering.py`; services `offering_service.py` (CRUD, `#seq`, cap, `format_price`, text cleaning), `offering_images.py`, `offering_media.py` (renditions, `destroy_assets`), `offering_import.py`, `offering_settings.py`; `app/workers/offering_cleanup.py` (orphan photo sweep, 90-day miss sweep); `scripts/capture_offerings.py`.

**Shared files edited (each one a small, additive change):** `plan_catalog.py` (values and feature line), `entitlements.py` (`FeatureDef("offerings")`, `usage()`), `subscription_validation_service.py` (two checks), `account_purge_service.py`, `models/{subscription_plan,tenant,notification_log,__init__}.py`, `schemas/{pricing,tenant,entitlements,lead}.py` (the parity tests require the columns to appear in the plan and tenant schemas), `lead_service.py` + `endpoints/leads.py` (`interests`), `utils/cloudinary_client.py` (`validate_image(allowed_formats, max_dimension)` with unchanged defaults for chat, `upload_offering_image`, `list_tagged_images`, `offering_sweep_tag`), `utils/http_rate_limiter.py`, `core/config.py`, `workers/document_processor.py` (registers the two sweeps), `main.py` (handler), `api/v1/api.py` (router), `scripts/seed_staging_e2e.py`, `tests/fakes/live_stub.py`. Outside the plan's S1 ownership list: `main.py`, `api.py`, the schema files, `lead_service.py`, `leads.py`, `cloudinary_client.py`, `http_rate_limiter.py`, `config.py`, `document_processor.py`, `live_stub.py`; each is the minimum the task needs.

**Migration `of10offerings`** (on `qv10quotevisitoremail`, single head): six tables; `max_offerings` (NULL = unlimited) and `max_offering_images` (NOT NULL, default 4) on `subscription_plans` and `tenant_capabilities`; plan values Trial 25/4, Lite 100/4, Professional 500/8, Growth 2,000/8, Enterprise unlimited/8; the feature line; a tenant backfill that skips a field whose key is in `override_flags`; `ALTER TYPE notif_event ADD VALUE 'offering_request'`. Idempotent (checks before every create, so a `create_all`-built database upgrades cleanly), `lock_timeout 5s`, reversible.

**Worker crons added** (daily, UTC): orphan photo sweep 03:45, Not-found 90-day sweep 03:50.

## Verification
* Unit/API tests (real Postgres 15 via the suite's fixtures): `tests/offerings/` (crud, images, import, import parse, settings, lead interests, concurrency, media, format), `tests/db/test_offering_models.py`, `tests/migrations/test_of10_offerings.py` (migrated tables **equal** the model-built tables: columns, indexes, uniques, FKs, checks; up/down/up; idempotent over `create_all`), `tests/entitlements/test_offerings_limits.py`, `tests/utils/test_cloudinary_offering_upload.py`, `tests/workers/test_offering_cleanup.py`, plus the purge completeness guard and three new purge tests.
* Mutation checks (tests fail when the code is broken): removing the row lock fails the concurrency test; disabling the atomic-apply and cap checks fails four import tests.
* Migration also run through the real `alembic upgrade head / downgrade -1 / upgrade head` CLI on a fresh Postgres 15 database.
* **Live run (T8)** against the running app (uvicorn API + arq worker + Postgres 15 + Redis 7): 76 captures, no 5xx. The real worker ran `sweep_old_offering_misses` from the arq queue (`Offering miss sweep: 1 deleted`). Stand-ins: Cloudinary uploader and image moderation (so real optimisation and real moderation are **not** proven); Not-found rows and a lead's wanted items were inserted with SQL because their writer is S2.
* Findings fixed during the live run: the "not an image" message exposed a Python object repr (now a stable sentence, test added); a capture scenario that I had miscounted (not an app bug).
* CI-equivalent fast checks: `ruff check app/`, `black --check app/`, the suites above, `tests/entitlements`, `tests/whatsapp`, chat image tests, the purge suite (701 + later runs green locally).

## Review hardening (independent security review of PR #195, 2026-10-31)
| Finding | Fix | Test |
|---|---|---|
| **High:** the `lines` import regex was quadratic on whitespace (about 9 minutes at the 256 KB cap, GIL held) | whitespace collapsed first (linear), a line over 1,000 characters is `422 line_too_long`, amounts over 30 characters are not parsed; `ImportRequest.content` capped at 1,048,576 characters | `test_review_fixes.py` (timing-bounded) |
| `PATCH` `null` on `kind`/`price_mode`/`availability` was a 500 | `422 {field}_required`; price rules skipped until the mode is valid | `..._required_enum_to_null...` |
| `quantity` over 2,147,483,647 was a 500 (Integer column) | `quantity_invalid` for 0..2,147,483,647 on create, patch and CSV | `..._out_of_range_quantity...` |
| Import compared RAW refs and names, then stored cleaned text (500 on a unique clash, upsert/add bypass, formula guard bypass) | `clean_text` runs inside `normalize_row` before any comparison; the formula guard moved into `validate_candidate`, so CRUD create/patch also reject `= + - @` names (a **behaviour change** for CRUD: `name_formula`); a database refusal on apply is `409 import_conflict` | `..._invisible_characters...`, `..._formula_guard...` |
| A test claimed EXIF stripping that it did not prove | assertion and comment corrected; `strip_profile` added; verification listed as open above | `test_cloudinary_offering_upload.py` |
| Unbounded storage (create, photo, archive loop) | uploads to archived items refused (`409 offering_archived`, also re-checked under the item lock); total rows ceiling `offering_archive_full` (4 x item cap, min 200, 20,000 unlimited) on create and import; per-tenant upload limiter (300/hour) next to the per-IP one | `..._archived_item_takes_no_new_photos`, `..._ceiling...`, `..._per_tenant` |
| Upload ran blocking DB and Pillow work on the event loop and held a transaction through moderation | `_admit` (gate, validation, Pillow) and `_store` (lock, insert) run in the thread pool; the connection is released (commit) before moderation | `..._off_the_event_loop...` |
| Sweep could use unbounded Admin API calls | 20-page budget per run, logged when reached | `..._sweep_stops_after_a_page_budget...` |

Accepted, documented: an item hard-deleted while its photo uploads returns 404 and the asset is destroyed right away (best effort; the 24 h sweep is the backstop). Item text reaches the model prompt in S2: `clean_text` keeps newlines in descriptions, so S2 must fence item text as data.

## Operate / roll back
* Deploy the API **and** the worker together (the worker registers two new crons; an old worker simply lacks them). New optional env var `CLOUDINARY_OFFERINGS_FOLDER`.
* The migration is additive but the **tenant backfill rewrites two columns on every `tenant_capabilities` row** (the plan's standing note recommends a pre-deploy dump for migrations that rewrite existing rows). **Decided 2026-10-31 (Adebayo): no pre-deploy dump.** The backfill only sets two new columns from the plan value (skipping overridden fields) and the downgrade drops them, so no existing value is lost.
* Rollback: `alembic downgrade qv10quotevisitoremail` drops the six tables (and every catalog row and photo row in them), both plan columns and the feature line. The `offering_request` enum label stays (Postgres cannot drop an enum value) and is harmless. Photo files already in Cloudinary are not removed by a downgrade; the orphan sweep would reap them only while the tables exist, so destroy them by tag `offering_<environment>` if a rollback is permanent.
* Kill switches: none needed in this stream (nothing reads the catalog yet); `tenant_offerings_settings.enabled` is stored for S2.

## Follow-ups
* Expose the per-item photo limit next to `limit`/`used` on the entitlements entry (needs a decision: changes a shared response).
* The lapsed-account `403` text is the generic plan-gate wording ("Upgrade to Trial to use it"); the FE guide tells the dashboard not to show it; a tailored message is a backend follow-up.
* Run the first upload against the real Cloudinary account and confirm the stored `bytes`, the 1,600 px edge and EXIF removal on real phone photos.
* **EXIF/GPS stripping is NOT verified.** The upload sends `flags: strip_profile` in the incoming transformation; `image_metadata=False` only stops the response echoing metadata and strips nothing (the first version of the test wrongly claimed it did). Staging check: upload a JPEG carrying GPS EXIF, download the stored original (`url`) and confirm with `exiftool` that no GPS or camera tags remain.
* Cloudinary Admin API quota: the orphan sweep lists 100 assets per call and now reads at most 20 pages (2,000 assets) per run; the quota (about 500 calls/hour) is shared with the video sweep. A backlog bigger than that clears over several nights; raise `MAX_SWEEP_PAGES` only with the quota in view.
* nginx `client_max_body_size` for the API is not in this repo; multipart bodies are parsed before auth, so the real bound on an upload body is the proxy's. The lead is checking the VPS config.
* Rate-limit behaviour (`429`) of the new upload limiter was not exercised live.
* Cloudinary plan limits (owner pre-check in the plan) remain unread; storage totals in the spec are arithmetic.
