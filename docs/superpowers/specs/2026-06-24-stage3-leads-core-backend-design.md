# Stage 3 — AI Sales Agent · Slice 1 "Leads Core" — Backend Design

**Date:** 2026-06-24
**Repo:** `luran` (FastAPI · SQLAlchemy async · Pydantic · Poetry · pytest)
**Paired FE spec:** `luran-dashboard/docs/superpowers/specs/2026-06-24-stage3-leads-core-frontend-design.md`
**Status:** Approved (brainstorm) → ready for implementation plan

---

## 1. Context & scope

Stage 3 (AI Sales Agent) is decomposed into five shippable slices. This spec is **Slice 1 — Leads Core**, the tenant-facing "see, score, and manage captured leads" loop. It lights up the already-existing-but-dormant `Lead` model (`app/db/models/lead.py`) and adds the surrounding behavior: a **lead-scoring engine**, **lead read/list/update endpoints**, and a **tenant sales-agent config** model.

**Build order (locked):** Leads Core → Chat-widget sales (intent + conversational capture) → Meeting booking → Quotation generator → Notifications + CRM push.

**In scope (BE):**
- Lead scoring engine (8 PRD signals → 0–100 + frozen breakdown), persisted on write, recompute endpoint
- `Lead.score_breakdown` JSON column (additive migration)
- Leads endpoints: list (filter/sort/paginate), summary, detail, status patch, recompute
- `TenantSalesAgentConfig` model + GET/PATCH endpoints
- Scoring hooked into the existing lead-creation path (public auditor capture)

**Out of scope (later slices):** conversational lead capture from chat (Slice 2 — only the prep exists in `chat.py`), intent-detection classifier, meeting model + calendar, quote model + PDF, in-app/Slack notifications, CRM connector. Lead fields `meeting_id`, `crm_id` stay null in Slice 1; the FE renders those panels as empty states.

**API versioning:** additive only, stays on `/api/v1`, built as a self-contained `sales`/`leads` module (per the project's API-versioning decision).

---

## 2. Data model

### 2.1 `Lead` (exists — additive change only)
`app/db/models/lead.py` already defines: `id`, `tenant_id` (nullable for public), `conversation_id`, `name`/`email` (EncryptedString PII), `email_hash`, `company`, `team_size`, `industry`, `intent`, `lead_score` (nullable int), `status` enum (`NEW`/`CONTACTED`/`QUALIFIED`/`CONVERTED`), `source`, `utm` (JSON), `crm_id`, `meeting_id`, `audit_report_id`, `created_at`.

**Change:** add one column.
```
score_breakdown: Mapped[dict | None] = mapped_column(JSON, nullable=True)
```
Stores the frozen list of signal results that produced `lead_score`, so the number shown in the UI always matches its explanation. Alembic migration `*_add_lead_score_breakdown.py` (additive, nullable — no backfill required; `lead_score`/`score_breakdown` populate on next write or recompute).

### 2.2 `TenantSalesAgentConfig` (new)
`app/db/models/sales_agent_config.py` — one row per tenant.

| field | type | notes |
|---|---|---|
| `id` | UUID PK | |
| `tenant_id` | UUID FK → tenants, **unique** | one config per tenant |
| `agent_enabled` | bool, default false | master switch |
| `lead_capture_enabled` | bool, default true | Slice 1-relevant |
| `meeting_booking_enabled` | bool, default false | inert until Slice 3 |
| `quotation_enabled` | bool, default false | inert until Slice 4 |
| `crm_provider` | str nullable | inert until Slice 5 (`hubspot`/`zoho`/`salesforce`/`webhook`) |
| `calendar_provider` | str nullable | inert until Slice 3 |
| `notify_channels` | JSON | `{email:bool, slack:bool, in_app:bool}`, default email only |
| `notification_email` | str nullable | |
| `trigger_intents` | JSON list | default `["PRICING_INQUIRY","DEMO_REQUEST","PURCHASE_INTENT"]` |
| `qualification_questions` | JSON list[str] | 3 PRD defaults seeded |
| `pricing_tiers` | JSON list | `[{name, price, unit, range, ...}]`, **currency NGN** |
| `currency` | str, default `"NGN"` | Luran's own surface → ₦ (cross-cutting X-1) |
| `created_at`/`updated_at` | datetime | |

Migration `*_create_sales_agent_config.py`. First read returns a default (not-yet-persisted) config; PATCH upserts.

---

## 3. Scoring engine

`app/sales/scoring.py` — pure, dependency-free, unit-testable.

```
SIGNALS = [
  ("provided_email",        "Provided email",                 20),
  ("company_name",          "Mentioned company name",         10),
  ("team_over_50",          "Team size > 50",                 15),
  ("enterprise",            "Enterprise / corporate",         20),
  ("asked_features",        "Asked about specific features",  10),
  ("requested_demo",        "Requested a demo",               15),
  ("mentioned_competitor",  "Mentioned a competitor",         10),
  ("ready_to_buy",          'Said "ready to buy"',            25),
  ("paid_ad",               "Came from a paid ad (UTM)",       5),
]  # note: 9 PRD signals; total possible 130 → capped at 100
```

`score_lead(lead, signals: dict[str,bool] | None) -> ScoreResult` where `ScoreResult = {score:int, tier:str, breakdown:list[{key,label,weight,fired:bool}]}`.

- **Field-based signals** (computed from `Lead` now): `provided_email` (email present), `company_name` (company present), `team_over_50` (team_size > 50), `enterprise` (industry/company matches enterprise/bank/corporate heuristic), `paid_ad` (utm has a paid source/medium).
- **Conversation-derived signals** (`asked_features`, `requested_demo`, `mentioned_competitor`, `ready_to_buy`): read from the optional `signals` dict. In Slice 1 this dict is empty/false (capture lands in Slice 2), so these never fire yet — the breakdown still lists them as not-fired (matches the mockup, which shows two grey rows).
- Score = `min(100, sum(weights of fired))`. Breakdown always returns **all** signals.
- **Tiers** (single source of truth, shared with FE): `cold < 31`, `warm 31–60`, `hot 61–85`, `sales_ready >= 86`. Expose `tier_for(score)` and the threshold constants.

`LeadService.apply_score(lead, signals=None)` calls the engine and writes `lead.lead_score` + `lead.score_breakdown`. Called on create and on update; `POST /leads/{id}/score` re-runs it.

---

## 4. Endpoints

New router `app/api/v1/endpoints/leads.py` (mounted at `/api/v1/leads`) and `app/api/v1/endpoints/sales.py` (`/api/v1/sales`). Schemas in `app/schemas/lead.py` and `app/schemas/sales_config.py`. All tenant-scoped via the existing auth dependency; cross-tenant access → 404 (`LEAD_NOT_FOUND`), mirroring the auditor entry-detail handler.

| Method | Path | Purpose | Response |
|---|---|---|---|
| GET | `/leads` | List tenant leads. Query: `status`, `intent`, `tier`/`min_score`, `sort` (`score`\|`captured`, default `score` desc), `limit`, `offset` | `LeadListItem[]` + paging meta |
| GET | `/leads/summary` | Stat cards + status counts (filter-independent) | `new_this_week`, `avg_score`, `sales_ready_count`, `pipeline_value` (0 in Slice 1), `status_counts{}` |
| GET | `/leads/{id}` | Full detail | `LeadDetail` |
| PATCH | `/leads/{id}` | Update `status` (validated transition) | `LeadDetail` |
| POST | `/leads/{id}/score` | Recompute + persist score/breakdown | `LeadDetail` |
| GET | `/sales/config` | Tenant sales-agent config (default if none) | `SalesConfig` |
| PATCH | `/sales/config` | Upsert config | `SalesConfig` |

**Schemas:**
- `LeadListItem`: `id, name, company, intent, lead_score, tier, status, source_label, created_at`. `source_label` derived from `source` + `utm` (e.g. "/pricing · Paid ad"). PII (`name`) decrypted server-side per existing pattern.
- `LeadDetail`: list fields + `email, team_size, industry, conversation_id, score_breakdown[], source{landing_page, utm, channel, first_seen}, meeting(null), quote(null), crm{connected:false}`.
- `SalesConfig`: full config mirror (§2.2).
- `LeadStatusPatch`: `{status}`; reject illegal transitions (e.g. cannot skip backward unless explicitly allowed) → 422 `INVALID_STATUS_TRANSITION`.

**Transcript:** not a new endpoint. `LeadDetail.conversation_id` lets the FE fetch the transcript via the existing `GET /chat/sessions/{id}/messages`. Null when no conversation is linked → FE shows an empty state.

---

## 5. Scoring trigger / integration

`app/api/v1/endpoints/public_auditor.py` already creates `Lead(source="public_auditor", ...)` on email capture. Hook `LeadService.apply_score(lead)` into that path so newly captured leads carry a score immediately. Existing rows (pre-migration) get scored lazily on first `GET /leads/{id}` (compute-if-null) or via `POST /leads/{id}/score`. No data migration needed.

---

## 6. Error handling

- Cross-tenant or missing lead → **404** `LEAD_NOT_FOUND` (never 403 — don't leak existence).
- Illegal status transition → **422** `INVALID_STATUS_TRANSITION`.
- Config PATCH validation (unknown notify channel, malformed pricing tier) → **422** with field detail.
- Scoring is total — never raises on partial data; missing fields simply don't fire their signals.

---

## 7. Testing (pytest)

- **Scoring unit** (`tests/sales/test_scoring.py`): each signal fires correctly; cap at 100 when sum > 100; tier boundaries (30/31, 60/61, 85/86); breakdown always returns all signals with correct `fired`.
- **Leads API** (`tests/api/test_leads.py`): list returns only caller's tenant; `status`/`intent`/`tier` filters; sort default score-desc; pagination; summary numbers; detail 404 cross-tenant; PATCH status valid + rejected transition; recompute updates persisted score+breakdown.
- **Sales config** (`tests/api/test_sales_config.py`): GET returns default when none; PATCH upserts and round-trips; second GET returns persisted; validation errors.
- Reuse existing fixtures (tenant, auth, encrypted-PII helpers). Target: green `poetry run pytest`.

---

## 8. Files

**New:** `app/sales/__init__.py`, `app/sales/scoring.py`, `app/db/models/sales_agent_config.py`, `app/schemas/lead.py`, `app/schemas/sales_config.py`, `app/api/v1/endpoints/leads.py`, `app/api/v1/endpoints/sales.py`, `alembic/versions/*_add_lead_score_breakdown.py`, `alembic/versions/*_create_sales_agent_config.py`, `tests/sales/test_scoring.py`, `tests/api/test_leads.py`, `tests/api/test_sales_config.py`.

**Modify:** `app/db/models/lead.py` (+`score_breakdown`), `app/db/models/__init__.py` (register config model), `app/api/v1/api.py` (mount leads + sales routers), `app/api/v1/endpoints/public_auditor.py` (apply score on capture), a `LeadService` (`app/services/lead_service.py`) for list/detail/patch/score orchestration.

---

## 9. Acceptance

1. New leads are scored on creation; score + breakdown persist and the breakdown sums to the score.
2. `GET /leads` returns only the caller's tenant, honoring filters/sort/pagination, with correct summary stats.
3. `GET /leads/{id}` returns full detail incl. breakdown and `conversation_id`; cross-tenant → 404.
4. `PATCH /leads/{id}` advances status; illegal transitions rejected.
5. `POST /leads/{id}/score` recomputes deterministically.
6. `GET`/`PATCH /sales/config` round-trip a per-tenant config with ₦ pricing; default returned when none.
7. All new tests green; no regression in existing suite.
