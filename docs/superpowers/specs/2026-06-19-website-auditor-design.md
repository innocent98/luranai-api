# Stage 4 — AI Website Auditor — Design Spec

**Version:** 1.0
**Date:** 2026-06-19
**Status:** Approved (brainstorm) — pending implementation plan
**Scope:** Stage 4 of the Luran Growth program (Stages 3–6). First build in the locked order: **Auditor → Sales → Intelligence → Marketing**.
**Source of truth:** `docs/luran_prd.md` (PRD Part 3, Stage 4) + the Claude Design board "Growth platform (Stages 3–6)" growth section (7 auditor screens).

---

## 1. Summary

The Website Auditor lets a business point Luran at a URL and, instead of manually uploading documents, have Luran **crawl the site, understand what the business is and does, discover all public pages, extract and adaptively classify content, detect missing content, score AI-readiness, and import a knowledge base** — with plan-aware gating. It runs in two modes from one engine:

- **Public acquisition tool** (`luranai.com/audit`, no auth): limited 50-page crawl, email-gated teaser report, top-of-funnel lead capture.
- **Dashboard tool** (signed-in tenant): full crawl (plan-capped), review + one-click import into the existing RAG knowledge base, re-scan anytime.

The defining refinement over the PRD: **no fixed content taxonomy.** A first LLM pass builds a structured `BusinessProfile` ("what this business is/does"), and every downstream step — classification, expected-content map, gap detection, recommendations — adapts to *that specific business* rather than assuming a generic pricing/product/FAQ checklist.

---

## 2. Locked decisions (from brainstorm)

1. **Full `Lead` model is created in this build.** The auditor only populates auditor-relevant fields (email, source, UTM, `audit_report_id`, `conversation_id`); scoring/CRM/meeting/status fields exist but stay null until Stage 3 implements the behavior.
2. **`BusinessProfile` is a first-class per-tenant entity and is injected into chat grounding in this build** (additive to the `RAGService` system prompt, behind a guard, with regression tests).
3. **Expected-content map is profile-driven now, conversation-refined over time.** `gap = expected − found`; recommendations ranked by `impact × demand`; no external/benchmark corpus. The conversation-refinement input is built now but dormant until chat data accrues (ties to Stage 5).
4. **Architecture = Approach ①:** self-contained `app/auditor/` logic package + **separate staging tables** (`AuditReport`, `AuditEntry`); a **single multi-phase ARQ job**; RAG (`KnowledgeSource`/`KnowledgeChunk`) is touched **only at import**.
5. **Adaptive categories:** each entry carries a free-form business-specific `category` **plus** a normalized `kind`; `kind`→ existing `source_type` mapping avoids migrating the existing enum.
6. **Model seam:** all auditor LLM calls go through one task-named `llm.py` interface so a per-task model swap (Gemini/DeepSeek/etc.) is config, not refactor. Default: gpt-4o for reasoning tasks, gpt-4o-mini for per-page extraction/classification.
7. **Crawler = Python Playwright as an ARQ worker** (single-stack; PRD's Node/BullMQ note overridden).
8. **Claim on signup = incremental resume, not re-run.** Reuse the persisted `BusinessProfile` + already-extracted public `AuditEntry` rows; crawl only the not-yet-scanned remainder up to the plan cap; recompute the expected-map → gap → score once over the union. **Fresh full crawl only when the first scan is older than 7 days or the staged data was purged.** The tenant "Re-scan" button is always available.
9. **"Generate outline" / "Write it now"** on the Review-gaps screen are **Stage 6 (Marketing)** functionality → delivered here as forward-stubs; Stage 4 provides the gap + `recommended_content` payload only.
10. **Currency:** the auditor makes **no currency assumptions** — extracted pricing stores the currency as found. Naira (₦) framing is a Stage 3/dashboard concern tracked in the design punch-list.

---

## 3. Module layout

Logic lives in a cohesive package; models/schemas/endpoints follow existing repo conventions.

```
app/auditor/                  # the self-contained module (logic)
  crawler.py                  # Playwright render+fetch, robots, politeness, caps
  discovery.py                # sitemap.xml → BFS link discovery, structural prune, SSRF guard
  extraction.py               # main-content extraction (readability/trafilatura), content_hash
  profile.py                  # BusinessProfile synthesis (LLM)
  classify.py                 # adaptive per-page category + normalized kind (LLM)
  gaps.py                     # expected-content map + gap analysis + recommendations
  scoring.py                  # Knowledge/Support/Conversion/Overall + projected (pure)
  pipeline.py                 # orchestrates phases, streams ProcessingJob progress
  llm.py                      # task-named model seam (profile_synthesis/page_classify/gap_analysis)
  service.py                  # public API: start_audit, import_entries, claim, rescan, purge
app/db/models/
  business_profile.py         # BusinessProfile
  audit.py                    # AuditReport, AuditEntry
  lead.py                     # Lead (full Stage 3 schema)
app/schemas/auditor.py
app/api/v1/endpoints/auditor.py        # tenant (authed) routes
app/api/v1/endpoints/public_auditor.py # public (no-auth) routes
app/workers/                  # register `website_audit` job + unclaimed-purge cron in WorkerSettings
alembic/versions/             # migrations for the new tables + new capability columns
```

---

## 4. Data model

All tables multi-tenant; PII fields use the existing `EncryptedString` convention. New models:

### 4.1 `BusinessProfile` (one per tenant)
- `id`, `tenant_id` (FK, unique)
- `summary` (text), `industry` (str), `offerings` (json), `audience` (json), `positioning` (text), `key_entities` (json), `tone` (json, advisory — defers to `TenantAIConfig.brand_voice`)
- `source` (enum: auditor|manual), `confidence` (float), `model_version` (str), `raw` (json — full LLM output for debug)
- `created_at`, `refreshed_at`
- *Not PII → not encrypted.*

### 4.2 `AuditReport`
- `id`, `tenant_id` (nullable — public pre-signup), `url`, `domain`, `scope` (enum: public|dashboard)
- `status` (enum: queued|crawling|analyzing|completed|failed)
- `pages_discovered` (int), `pages_scanned` (int)
- `business_profile_id` (FK, nullable)
- `knowledge_score`, `support_score`, `conversion_score`, `overall_score`, `projected_score` (int 0–100)
- `gaps` (json: list of `{title, why_it_matters, recommended_content, kind, demand_count}`)
- `summary_stats` (json: counts per category/kind)
- `imported` (bool), `imported_at`
- `email` (nullable, encrypted), `lead_id` (FK, nullable), `claim_token` (str, indexed)
- `processing_job_id` (FK → `ProcessingJob`)
- `created_at`, `completed_at`

### 4.3 `AuditEntry` (staged extracted content — never in RAG until import)
- `id`, `audit_report_id` (FK), `tenant_id` (nullable until claimed)
- `url`, `title`
- `category` (str, free-form, business-specific), `kind` (enum: faq|policy|product|pricing|guide|contact|testimonial|other)
- `content` (text), `content_hash` (str)
- `importance_rank` (int)
- `status` (enum: proposed|active|locked|saved|imported|discarded)
  - `active` = selected within plan limit · `locked` = over-limit (saved-not-discarded) · `imported` = materialized to RAG · `discarded` = tenant-deleted
- `knowledge_source_id` (FK, nullable — set on import)
- `meta` (json: section, page, extraction_confidence, currency-as-found)
- `edited` (bool), `created_at`, `updated_at`

### 4.4 `Lead` (full Stage 3 schema; auditor populates a subset)
- `id`, `tenant_id`, `conversation_id` (nullable)
- `name` (enc), `email` (enc) + `email_hash`, `company`, `team_size`, `industry`
- `intent` (str), `lead_score` (int, nullable), `status` (enum: new|contacted|qualified|converted)
- `source` (str — e.g. `public_auditor`), `utm` (json)
- `crm_id` (nullable), `meeting_id` (nullable), `audit_report_id` (FK, nullable)
- `created_at`, `updated_at`

### 4.5 Reuse / changes to existing models
- `ProcessingJob`: new `job_type='website_audit'`; `total_items=pages_discovered`; per-page progress → drives the Scanning screen over the existing WS channel.
- `TenantCapabilities` + `SubscriptionPlan`: **add** `max_audit_pages_per_crawl` (e.g. 50/200/500) and `max_audits_per_month` (drives "2 of 10 used"). Active-import limit **reuses existing `max_knowledge_sources`**.
- `KnowledgeSource`: no schema change. On import, `kind`→`source_type` map: `faq→faq`, `guide→help_doc`, `policy→policy`, `product|pricing|contact|testimonial→other` (original `category` preserved in `meta`). `source='auditor'`.

---

## 5. Pipeline (single `website_audit` ARQ job)

Phases stream progress into `ProcessingJob` (the Scanning screen's 6-step checklist):

1. **Discover (`discovery.py`)** — normalize+validate root URL; **SSRF guard (`network_security.resolve_and_validate_host`) on root and every discovered URL** (resolve-and-pin; blocks private/loopback/metadata); fetch+respect `robots.txt`; polite **1 req/sec/domain**; `sitemap.xml` first → BFS depth-3 fallback; **structural prune only** (drop login/checkout/cart/admin/asset/query-dup — relevance is decided later, intelligently); record `pages_discovered`; stop at cap (public 50 / dashboard plan).
2. **Render + extract (`crawler.py`, `extraction.py`)** — Playwright headless (JS + static), 10s/page timeout, content-size cap; readability/trafilatura main-content extraction; `content_hash` dedup.
3. **BusinessProfile synthesis (`profile.py`, gpt-4o)** — token-budgeted sample (home/about/top pages) → structured `BusinessProfile`; persisted; **reused on re-scan unless content materially changed** (stabilizes scoring).
4. **Adaptive classification (`classify.py`, gpt-4o-mini, batched)** — each page classified **with `BusinessProfile` as context** → free-form `category` + normalized `kind` + `importance_rank` + keep/skip relevance; emits `AuditEntry(status=proposed)`. No fixed taxonomy.
5. **Expected-content map (`gaps.py`, gpt-4o)** — from `BusinessProfile`, generate what a business like this *should* publish to serve+convert; each item tagged with `{dimensions:[knowledge|support|conversion], weight}`. Accepts optional `unanswered_questions` (empty at onboarding; populated from chat later).
6. **Gap analysis + recommendations (`gaps.py`, gpt-4o)** — `gaps = expected − found`; each gap `{title, why_it_matters, recommended_content, kind, demand_count}`; ranked by `impact × demand`.
7. **Scoring (`scoring.py`)** — see §6.
8. **Persist** `AuditReport` + entries; mark `ProcessingJob` complete; notify (in-app + email per PRD 6.3).

**Determinism guardrails:** low temperature on profile/gap calls; persisted+reused profile; bounded scoring rubric (arithmetic, not an LLM number). Same unchanged site → same score.

---

## 6. Scoring (`scoring.py` — pure, deterministic)

The LLM sets the **rubric** (during the expected-map build); arithmetic computes the **number**:

- Each expected item: `{item, dimensions, weight}`.
- `dimension_score = Σ(weight of expected items FOUND) / Σ(weight of all expected items)` per dimension → **Knowledge coverage**, **Support readiness**, **Sales conversion ready** (0–100).
- **Overall** = weighted blend: knowledge 0.40 · support 0.35 · conversion 0.25.
- **`projected_score`** = same arithmetic assuming the recommended gap items are added → the credible "74 → 91".

`scoring.py` is a pure function of (expected_map, found_set) → fully unit-tested with fixtures, no LLM, no network.

---

## 7. Import + plan-gating (`service.import_entries`, dashboard only)

- Entries pre-ranked by `importance_rank`. Active slots = `max_knowledge_sources` (e.g. Free=3). Top-N pre-selected `active`; remainder `locked` (saved-not-discarded). Tenant may swap actives within limit, edit inline (`edited=true`), delete (`discarded`).
- **Confirm →** each `active` entry creates a `KnowledgeSource(source='auditor', source_type=mapped kind)` + enqueues embedding via the **existing `document_processor`**; `AuditEntry.status='imported'`, `knowledge_source_id` linked. Locked entries remain staged.
- **Idempotent** per `(audit_report_id, entry_id)` (reuses the idempotency-key pattern) — re-import never double-creates sources.
- **Re-scan dedup:** `content_hash`+`url` match against already-imported entries → powers "Skip pages already in my knowledge base"; matches update-in-place (version bump), no duplicate.
- **Upgrade unlock:** when `max_knowledge_sources` rises, locked entries become eligible ("activate N more"); activation stays tenant-confirmed.

---

## 8. Public → Lead → claim path

- **Public audit:** no tenant; `scope=public` (50 pages). Report exposes only scores + counts + **blurred** gap titles — never raw entry contents.
- **Email capture** ("get the full report by email"): create `Lead(source='public_auditor', email, utm, audit_report_id)`; stamp `AuditReport.email/lead_id`; email the full *report* (scores + named gaps + recs) via `EmailService`. Import still requires signup.
- **"Create free account to import":** signup carries `claim_token` → on tenant creation: re-parent `AuditReport` + `AuditEntry` to `tenant_id`, attach `BusinessProfile` to the tenant, and **incrementally resume** (see §2.8) — reuse profile + existing entries, crawl only the remainder up to plan cap, recompute expected-map→gap→score over the union. Lands on Review & Import.
- **Staleness/fallback:** if the first scan is > **7 days** old (or staged data purged), claim does a fresh full crawl instead of resume.
- **Rate limit:** public tool 3 audits/IP/day (existing Redis limiter).
- **Purge:** periodic ARQ job drops public reports/entries with no `tenant_id` after **30 days (configurable)** ("nothing stored without consent"). Note: this is longer than the 7-day incremental-resume staleness window — a visitor can still claim after 7 days (they just get a fresh crawl instead of a resume), up until the 30-day purge removes the staged data entirely.

---

## 9. Chat-grounding wiring (`BusinessProfile` → `RAGService`)

- **Additive only**, behind a guard (profile exists + feature flag). Inject a compact "About this business" block (`summary` + `offerings` + `key_entities`, ~150-token fixed budget) into the system prompt **before** KB context. Distinct from `brand_voice` (style) and `custom_instructions`.
- **Regression safety:** prompt snapshot tests with/without profile; escalation + grounding behavior asserted unchanged; KB/history token budgets untouched; safe when profile absent (no-op).

---

## 10. Model seam (`llm.py`)

Single interface keyed by task name: `profile_synthesis`, `page_classify`, `gap_analysis`, `expected_map`. Each task maps to a configured `(provider, model)` pair. Defaults: gpt-4o (reasoning), gpt-4o-mini (per-page). Swapping a task to Gemini/DeepSeek later is a config change. (Model-provider exploration is a deferred, separate decision — this seam keeps it open without delaying Stage 4.)

---

## 11. Security

- **SSRF:** resolve-and-pin guard on every crawled URL (root + each discovered link), reused from the hardened action subsystem; blocks private/loopback/metadata + DNS-rebind.
- **Crawl safety:** robots.txt respected; 1 req/sec/domain; max pages cap; 10s/page timeout; content-size cap.
- **Public abuse:** 3 audits/IP/day; unclaimed-data purge; no raw content in public responses.
- **Tenant isolation:** all reports/entries/profiles scoped by `tenant_id`; RAG never sees staged/locked entries.

---

## 12. Screen → endpoint mapping (all 7 designed screens)

| Screen | Endpoint / data |
|---|---|
| Public input (`AuditorHero`) | `POST /api/v1/public/audit` (scope=public) |
| Scanning (`AuditorScanning`) | `ProcessingJob` progress over existing WS channel |
| Public report teaser (`AuditorReport`) | `GET /api/v1/public/audit/{id}` (scores + blurred gaps) · `POST /api/v1/public/audit/{id}/email` → Lead |
| Dashboard review & import (`DashAuditReview`) | `GET /api/v1/audit/{id}/entries` · `POST /api/v1/audit/{id}/import` |
| Run / re-scan (`DashAuditRun`) | `POST /api/v1/audit` (scope=dashboard) · `GET /api/v1/audit` (history) · plan limits |
| Edit entry (`DashAuditEdit`) | `PATCH /api/v1/audit/entries/{id}` |
| Review gaps (`DashAuditGaps`) | `GET /api/v1/audit/{id}/gaps` (+ forward-stub "Generate outline") |

---

## 13. Testing strategy

- **Pure unit (no LLM/network):** `scoring.py` (exact scores + projected from fixtures); `kind`→`source_type` table; `gaps.py` set math + impact×demand ranking + empty/populated `unanswered_questions`; plan-gating math + upgrade unlock.
- **Component (fixtures):** `discovery.py` (sitemap, BFS depth cap, prune, cap, robots, **SSRF guard invoked per URL**); `crawler.py`/`extraction.py` against a **local fixture site** (static + JS-rendered) for stripping, `content_hash`, timeout/size caps; LLM phases mocked via `llm.py` seam (shape, persistence, reuse-unless-changed).
- **Integration (real Postgres, mocked LLM, fixture site):** full pipeline e2e (report+entries+scores, `ProcessingJob` progress); import → real `KnowledgeSource`+chunks, retrieval **sees imported / never sees locked-staged**; public flow (3/IP/day, email→Lead, claim links report, incremental resume, >7-day fresh fallback, purge); chat grounding prompt snapshot with/without profile.
- **Determinism:** same fixture site twice → identical scores.
- **Security:** SSRF (private/loopback/metadata/DNS-rebind), per-IP rate limit, cross-tenant isolation.

---

## 14. Acceptance criteria (PRD Stage 4 + additions)

PRD §3.3 checkboxes:
- [ ] Crawler handles static + JS-rendered pages → `crawler.py` + fixture tests
- [ ] Content correctly classified → `classify.py` (adaptive) + `kind` map
- [ ] AI Readiness Report generates accurately → `scoring.py` (deterministic)
- [ ] Missing content detection reliable → `gaps.py`
- [ ] One-click import populates KB → `service.import_entries` + `document_processor`
- [ ] Review mode per-entry edit/select → `AuditEntry` status/edit + endpoints
- [ ] Public auditor works without auth → `public_auditor.py`
- [ ] Public rate limit 3/IP/day → Redis limiter
- [ ] After sign-up, full audit resumes automatically → incremental claim (§8)

Additions from brainstorm:
- [ ] `BusinessProfile` synthesized and injected into chat grounding (regression-safe)
- [ ] Fully adaptive taxonomy (no fixed content list)
- [ ] `Lead` model created; public email capture creates a `Lead`

---

## 15. Out of scope / forward boundaries

- **"Generate outline" / "Write it now"** (Stage 6 Marketing) — forward-stubs only here.
- **Conversation-refinement of the expected-map** — interface built now, dormant until Stage 5 chat data exists.
- **Native CRM (HubSpot) push, lead scoring, meeting booking, quotation** — Stage 3.
- **Naira currency UI conversion** — Stage 3/dashboard punch-list (`design-board/PUNCHLIST-stage3-6.md`).
- **Model-provider exploration (Gemini/DeepSeek)** — deferred; `llm.py` seam keeps it cheap to adopt.

---

## 16. Key entity diagram (text)

```
Tenant ─1:1─ BusinessProfile ──(inject)──> RAGService system prompt
   │                ▲
   │                │ synthesized by
   └─1:N─ AuditReport ──1:N── AuditEntry ──(import)──> KnowledgeSource ──> KnowledgeChunk (Qdrant)
                │                                   (active only; locked stay staged)
                ├── processing_job_id ─> ProcessingJob (progress → WS → Scanning screen)
                └── lead_id ─> Lead (public email capture; full Stage 3 schema)
```
