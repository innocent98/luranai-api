# Offerings catalog — build plan

**Date:** 2026-10-30 · **Spec:** `docs/superpowers/specs/2026-10-30-offerings-catalog-design.md` (read it first; section numbers below refer to it) · **Status:** DRAFT, nothing built; starts only after the owner approves the spec.
**Goal:** the pilot-ready slice (spec section 16) live on staging with a real WhatsApp number in about a week of wall time. **All durations below are estimates** (no measured velocity for this feature); they use "a Sonnet session-day" as the unit and are meant for ordering, not promising.

## 1. Streams at a glance

| Stream | Repo / branch (push to exactly this name; never `claude/...`) | Builds | Size | Depends on | Review |
|---|---|---|---|---|---|
| **S1 Backend core** | `luran` · `feature/offerings-backend-core` | tables, migration, plan limits, CRUD, photos, import, confirm, settings, entitlements meter, FE guide part 1 | **M** (1.5–2.5 d) | nothing | Sonnet per task; one Opus pass at the end of the phase (covers S1+S2+S4b) |
| **S2 Backend AI path** | `luran` · `feature/offerings-ai-grounding` | grounding, block, guard, schema variant, `send_message` integration, cart → interests → quote, quote text, `OFFERING_REQUEST`, lead source, eval | **L** (3–4 d) | S1 T1 (tables) | Sonnet per task; **Opus per task on the guard (S2-T4) and the quote/cart totals (S2-T6)** because they touch prices and money |
| **S3 Dashboard** | `luran-dashboard` · `feature/offerings-dashboard` (base `staging`) | Offerings page (Today list, sheet, photos, import, meter, Not-found, lead panel) | **L** (3–4 d) | S1 contract frozen (captures) | Sonnet; one Opus pass if time, otherwise Sonnet code-review |
| **S4a Item cards** | `luran-chat` · `feature/offerings-item-cards` (base `stage`) | `meta.offerings` mapping at 3 sites + `ItemCard` + gallery, fixture-driven | **S** (0.5–1 d) | the `meta.offerings` shape (spec 11, frozen below) | Sonnet |
| **S4b WhatsApp photos + quote text** | `luran` · `feature/offerings-whatsapp-photos` | `send_whatsapp_offering_image` job, photo cap, caption at send time, quote text message | **M** (1–1.5 d) | S1 T1; S2 T5 (`meta.offerings` written) for the end-to-end test | Sonnet; included in the Opus pass because the caption carries a price |
| **Integration + pilot gate** | all | staging deploy, eval against the real model, real-phone test, owner dry-run, pilot onboarding | **S–M** (1–2 d) | all above | the lead + owner |

**Critical path:** S1 → S2 → integration, about 6–8 working days. The dashboard (S3) runs beside S2, and S4a runs beside S1.

## 2. Order and parallelism (max 3 streams at once)

```
Day 0      owner pre-checks (section 6) ── S1 starts, S4a starts (fixtures only)
Wave 1     S1 backend core  ║  S4a item cards                        (2 streams)
           └─ S1 T1 merged/frozen (migration + models + plan columns) ─┐
Wave 2     S2 AI path  ║  S3 dashboard  ║  S4b WhatsApp photos         (3 streams)
Wave 3     integration on staging: eval ▸ real phone ▸ owner dry-run ▸ pilot #1
```

Rules that keep parallel work from colliding:
- **S1 alone owns the Alembic migration** `of10offerings` (all six tables incl. `offering_activity_days`, the plan columns on `subscription_plans` and `tenant_capabilities`, the new `NotifEvent` value, the `lead` interest table). S2 and S4b add **no** migration; this prevents two heads. (Builder re-checks the current head: `wa40contactexclusion` at design time.)
- File ownership: S2 owns `app/offerings/*` (new package), `app/api/v1/endpoints/chat.py`, `app/sales/intent.py`, `app/services/rag_service.py`; S1 owns `app/db/models/offering.py`, `app/api/v1/endpoints/offerings.py`, `plan_catalog.py`, `entitlements.py`, `subscription_validation_service.py`, `account_purge_service.py`; S4b owns `whatsapp_*`, `app/workers/whatsapp.py`. A change outside your files is a message to the lead.
- **Frozen cross-stream contract** (so S2/S3/S4 can proceed in parallel):
  - `message.meta.offerings` = `{mode: "browse"|"detail"|"none", items:[{ref:"#12", offering_id, name, price_text, availability, cover_image_id|null, cover_url|null, photo_count}], shown_at, guard:{checked, blocked, regenerated, fell_back}}` (max 5 items).
  - `meta.quote` unchanged (`{currency, line_items[{label,detail,amount}], total}`).
  - REST shapes in spec section 9; S1's FE guide with live captures supersedes the sketch.
- S3 may start the day S1 T3 (CRUD) is runnable: the dashboard works against S1's branch/staging and S1's captured responses.
- Backend stream commits stay on their own branches; PRs to `develop`; **no stream merges its own PR** (the lead merges after the owner's "merge #N").

## 3. Stream task lists (test-first; each task ends green on its own tests)

### S1 Backend core (`feature/offerings-backend-core`)
- [ ] **T1** Models `Offering`, `OfferingImage`, `TenantOfferingsSettings`, `LeadOfferingInterest`, `OfferingMiss`, `OfferingActivityDay` (`app/db/models/offering.py`, export in `models/__init__.py`); `NotifEvent.OFFERING_REQUEST`; Alembic `of10offerings` (single head; lock timeout; idempotent; reversible; round trip on scratch Postgres). *Freeze point for S2/S4b.*
- [ ] **T2** Plan limits: `max_offerings`, `max_offering_images` in `plan_catalog.py` (Trial 25/4, Lite 100/4, Professional 500/8, Growth 2,000/8, Enterprise null/8), generated `features` string, `FeatureDef("offerings", limit="max_offerings")`, `usage()`, tenant backfill in the migration (skip where `override_flags` has the key), parity test updated, `GET /tenant/entitlements` entry, downgrade guard in `SubscriptionValidationService` (`data_exceeds_limits`).
- [ ] **T3** `OfferingError` + handler in `main.py` (flat `{code,message}`); CRUD service + endpoints (`/api/v1/offerings`): create with `seq` allocation under row lock, list/filter/cursor, patch, archive/hard delete, cap enforcement (`offering_limit`), validation codes, lapsed-tenant write gate.
- [ ] **T4** Photos: `cloudinary_manager.upload_offering_image` (tenant folder), `validate_image` + `moderate_image` (fail-closed) + server re-encode ≤ 1,600 px (EXIF stripped), per-item photo cap, order, delete (asset destroyed), orphan sweep; extend `account_purge_service` and hard delete.
- [ ] **T5** Import: parser (`csv` and `lines`, amounts `12,000`/`₦12000`/`12k`, formula-leading cells rejected), preview (no writes) and apply (atomic, `skip_errors`), limits 500 rows / 256 KB, upsert on `external_ref` (and on case-insensitive name when `mode=upsert` and no ref; **decision for the builder**, document it).
- [ ] **T6** `POST /offerings/confirm`, settings GET/PATCH, misses list/dismiss (`offering_misses` write side is S2's), 90-day miss sweep job.
- [ ] **T7** Leads detail gains additive `interests[]` (verify the response is additive; write side is S2).
- [ ] **T8** Live run against the running app (API + worker + Postgres + Redis) capturing every request/response to `docs/fe-integration-captures/offerings/`; `docs/fe-integration-guide-offerings.md` part 1 (verification table: live vs not); SOP `docs/sop/offerings_backend_core.md`; checklist ticks; fast local checks; PR to `develop`.

### S2 Backend AI path (`feature/offerings-ai-grounding`)
- [ ] **T1** `app/offerings/grounding.py`: query building (current + previous short message + `session_meta["offerings_shown"]`), lexical search (spec 7.2), browse intent, ≤ 12 menu / ≤ 5 detail, 1,000-token hard cap, freshness line, stub block, fail-closed block; unit tests incl. token budget at 5,000 synthetic items and cross-tenant isolation.
- [ ] **T2** Prompt rules (spec 7.6) and the injection into `send_message` for any tenant (support and sales); grounded-turn handling so a catalog match counts as retrieval in the grounding policy; `message_channel` aware formatting. Tenants without active items: byte-identical prompt (a test pins this).
- [ ] **T3** `SALES_RESPONSE_SCHEMA_OFFERINGS` variant + `parse_sales_output` tolerance; existing schema and its tests untouched; verify no other consumer breaks (**first thing to check**, spec 17.2).
- [ ] **T4 (Opus review)** `app/offerings/guard.py`: amount extraction (Nigerian forms), allowed set, sentence rule with KB amounts, strict mode, one regeneration, code fallback reply, `meta.offerings.guard`; table-driven tests with ≥ 60 reply strings (haggling, "2k", "N2,500.50", KB fee next to an item name, stale KB price, customer-quoted amount).
- [ ] **T5** `meta.offerings` writer (structured refs in sales mode, name/ref match in plain mode), miss recording (normalised term only), `offerings_shown` session memory.
- [ ] **T6 (Opus review)** Cart → `lead_offering_interests` (snapshots), D1 `offered_price` + handover, catalog quote (all-`fixed` carts only; code totals; reuse `QuoteService`, PDF, `QUOTE_GENERATED`), `render_quote_text`, lead `source` from channel, `OFFERING_REQUEST` notification on first cart (email always; Slack/CRM under `crm_notifications`), display-only score signals. Tests: a cart with a `from` item produces no quote; Lite has interests and no quote; totals equal `Σ qty × price` from rows read after the reply.
- [ ] **T7** `scripts/offerings_eval.py` (≥ 40 adversarial cases, spec 15), runnable against the real model on staging, output committed as evidence.
- [ ] **T8** Live capture (web path and WhatsApp path with the fake sidecar), FE guide part 2 (`meta.offerings`, `meta.quote` for catalog, interests, errors), SOP `docs/sop/offerings_ai_grounding.md`, full local unit suite once (shared `chat.py`), PR.

### S3 Dashboard (`feature/offerings-dashboard`, base `staging`)
- [ ] **T1** `FeatureKey` `offerings`, nav item in the Build group (`B2BDashShell.tsx`, extend `lockedFeatures` only if a lock is needed), route `/dashboard/offerings`, types, `useOfferings` hooks (React Query + `apiClient`), entitlement meter.
- [ ] **T2** Today list (mobile-first cards, one-tap availability with Undo, confirm-today bar, filters); verified at 375 px.
- [ ] **T3** Add/edit sheet with the photo uploader (camera/gallery, reuse `lib/chat-attachments.ts` validation and downscale), plan photo meter, price modes, negotiable, services note.
- [ ] **T4** Import wizard (paste/CSV → preview → apply) and the empty state.
- [ ] **T5** Not-found tab with "Add this", and the lead detail "Wanted items" panel.
- [ ] **T6** Plan-cap states (`UpgradeNudge`, `offering_limit` / `offering_image_limit` errors), lapsed-tenant read-only state.
- [ ] **T7** Typecheck/lint/build and component tests on local Node 20 (Actions billing blocks CI, master handoff); screenshots at 375 and 1280 px; PR to `staging`.

### S4a Item cards (`luran-chat`, `feature/offerings-item-cards`)
- [ ] **T1** `meta.offerings` types + the three mapping sites (`useChat.ts mapResponseMessage`, `lib/public-support.ts`, widget `Message` type), history hydration check.
- [ ] **T2** `ItemCard` (+ price, availability chip, cover, photo count, gallery via the existing lightbox), wide-card rule in `MessageBubble`, "as of <time>" when the card is older than 1 h, tenant `primaryColor`.
- [ ] **T3** Fixture-driven tests; screenshots in the widget and hosted page; PR to `stage`.

### S4b WhatsApp photos + quote text (`feature/offerings-whatsapp-photos`)
- [ ] **T1** `enqueue_send_offering_image` + arq job `send_whatsapp_offering_image` (registered like `send_whatsapp_image`), re-read row at send time: same tenant, item `active`, image `https` on `WHATSAPP_MEDIA_URL_HOSTS`; caption from the live row (`#12 name · price · availability`), ≤ 1,024 chars; sidecar error mapping as `send_image`.
- [ ] **T2** Selection rule in `whatsapp_inbound` after the text is enqueued: 1–3 shown items, ≤ 3 photos per reply, ≤ 6 catalog photos per chat per hour (Redis limiter, fail-closed for photos only); text never waits on photos.
- [ ] **T3** `render_quote_text` as a second message after the reply (consumes S2's helper; stub until S2 T6 lands).
- [ ] **T4** Tests with `tests/fakes/fake_sidecar.py` (image allowlist, cap, dropped photo keeps text, cross-tenant id refused, archived item skipped); live run capture; SOP addendum to `whatsapp_media_and_rating.md`'s successor doc; PR.

## 4. Integration and pilot gate (the lead + owner, after the waves)

1. Merge order (owner's "merge #N" for each): S1 → S2 → S4b → S4a (luran-chat) → S3 (dashboard `staging`). Backend deploy and worker deploy together (new arq job).
2. Staging: seed one pilot-like catalog (20 items, photos) through the import UI.
3. **Eval gate:** run `scripts/offerings_eval.py` against the real model; 0 violations of rules 1–5 or no pilot.
4. **Real-phone test** with the owner's own number: browse, specific item with photo, sold-out, not-listed, haggling (D1), choose two items → lead + interests + quote text + `OFFERING_REQUEST` email; widget and hosted page cards; voice spot check of a price read-out (spec 17.6).
5. Prod promotion `develop` → `main` carries the migration (additive; no pre-deploy dump needed per the standing ruling, but `of10` backfills tenants, so a dump is **recommended** since it rewrites existing rows: owner's call).
6. Pilot #1 onboarded by the owner with a 15-minute screen-share (import their list, add photos, one confirm tap); audit query for the first two weeks (spec 15).

## 5. Standing rules for every stream (copy into each brief)

- No AI attribution in commits, PR bodies or comments; clean commits authored by Adebayo. No `claude/...` branch: push to the exact branch in the table. Opus reviews only where marked.
- Fast local checks before each push, with the project's toolchain (`poetry run ruff check app/`, `poetry run black --check app/`, type-check as the workflow runs it, tests for touched areas; migration round trip on a scratch DB for S1). CI runs the rest. S1 touches shared code (`plan_catalog`, `entitlements`): also run `tests/entitlements`. S2 touches `chat.py` (every channel): run the full unit suite once locally. Dashboard/chat: typecheck, lint, build on Node 20.
- Per stream, as part of finishing: an SOP in `docs/sop/`, the checklist ticks in `docs/checklist/master-checklist.md`, FE guide sections with **bodies copied from real captures** (`docs/fe-integration-guide-offerings.md`, captures in `docs/fe-integration-captures/offerings/`; placeholders obviously fake).
- Report back in about 15 lines with a ledger pointer; never merge.
- Max 2–3 agents at once; one heavy job at a time on this machine.

## 6. Owner / lead actions before or during the build

- [ ] Read the **Cloudinary plan's storage and bandwidth limits** (spec 12: the totals are arithmetic until this is known) before any Professional/Growth tenant fills its cap.
- [ ] Choose **three pilot businesses** and ask each for a real price list (text or photos) now: they become the dry-run catalogs and the eval fixtures.
- [ ] Fix GitHub Actions billing for `luran-dashboard` or accept local-only checks for S3 (master handoff).
- [ ] Apply the **PRD addendum** (spec section 5) to the workspace `docs/luran_prd.md` (outside this repo, so this PR cannot carry it).
- [ ] Decide later, not blocking: whether Lite should get quotes (separate pricing decision, D2); Terms wording for regulated goods (spec 13).
- [ ] Review the cap and photo numbers after the first pilot week (D3).

## 7. Risks to watch while building

1. The strict structured-output schema variant (S2 T3) may expose a consumer that rejects extra keys: first thing checked.
2. `chat.py` is shared by every channel and voice: S2's gate must leave tenants without items byte-identical; a pinned prompt test is the guard.
3. The guard is only as good as its amount extractor: the 60-string table test and the Opus review are the defence; any stop-ship price error during the pilot pauses the feature for that tenant (`tenant_offerings_settings.enabled=false`) while it is fixed.
4. WhatsApp photo bursts against the real sidecar caps are unmeasured at the 10-number ceiling: the integration real-phone test watches the cap counters.
5. Dashboard has no CI: the S3 checklist is manual, screenshots are the evidence.

## 8. Checklist mapping

Repo checklist (`docs/checklist/master-checklist.md`): a new module section "Offerings catalog" is added by this PR (design phase done, streams S1–S4 as sub-items, P1/v2 backlog, deferred follow-ups). **Workspace checklist** (`/Users/adebayovictor/Documents/dev/luran/docs/checklist/luran-master-checklist.md`, section "📋 Offerings catalog", currently two lines): replace the second bullet with: "spec + plan on `develop` via PR (docs only), owner approved the spec 2026-10-30 (D1 never below price; D2 caps 25/100/500/2,000/unlimited, quotes still `sales_close`; D3 photo limits 4/8, 1,000-token cap)"; add the five streams (S1 core, S2 AI path, S3 dashboard, S4a cards, S4b WhatsApp photos) as `[ ]` items with their branch names; add under Deferred: Cloudinary limits unknown, availability wording unchecked, PRD addendum pending in the workspace PRD, Lite quote decision. The lead applies this note (the workspace checklist is outside the repo).
