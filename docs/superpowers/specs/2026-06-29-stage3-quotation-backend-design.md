# Stage 3 — AI Sales Agent · Slice 4 "Quotation Generator" — Backend Design

**Date:** 2026-06-29
**Repo:** `luran` (FastAPI · SYNC SQLAlchemy · Pydantic · Alembic · pytest · OpenAI)
**Paired FE specs:** `luran-chat/.../2026-06-29-stage3-quote-card-widget-design.md` · `luran-dashboard/.../2026-06-29-stage3-pricing-config-design.md`
**Builds on:** Slice 1 (`Lead`, `LeadService`), Slice 2 (`app/sales/intent.py` `SALES_RESPONSE_SCHEMA`, sales-mode generation, chat orchestration), Slice 3 (`offer_meeting` piggyback pattern, `TenantSalesAgentConfig.availability`). **Branch `feat/stage3-quote-backend` stacks on `feat/stage3-meeting-backend` (Slice 3 PR #33).**
**PRD:** `docs/luran_prd.md` §3.5 (Quotation Generator), §3.6 (Sales config), acceptance §2.3.
**Status:** Approved (brainstorm) → ready for implementation plan

---

## 1. Context & scope

Slice 4 lets the chat sales agent **generate an exact, in-chat quotation** from the tenant's pricing config when the visitor has provided enough info, feeding the Slice 1 leads pipeline. Slice order: 1 Leads ✅ → 2 Chat sales ✅ → 3 Meeting ✅ → **4 Quotation (this)** → 5 Notifications+CRM.

**Decisions (brainstorm):**
- **Deterministic backend pricing engine** — the LLM only extracts inputs (team_size, plan_interest) and signals `offer_quote`; a pure pricing engine computes line items + total. The model never does arithmetic.
- **In-chat card only** — Slice 4 computes + persists a `Quote` and returns it in `meta.quote`; **PDF generation + email delivery are Slice 5.**
- **Compute inline, embed in `meta.quote`** — no separate endpoint (a quote is non-interactive, fully determined by known inputs; mirrors how `meta.lead_captured` rides the reply).
- **Pricing model:** tiers `{name, base_price, min_users, max_users, per_seat_overage_price}` + tenant `annual_discount_pct`; `currency` already on config.

**Out of scope (later):** PDF + email (Slice 5); "adjust the numbers" interactivity; add-ons / volume-discount thresholds / multi-currency.

**Constraint (PRD §291):** sales-off / `SUPPORT` / quotation-disabled path is the existing behavior, unchanged.

---

## 2. Data model

New table `quotes` (`app/db/models/quote.py`):

| column | type | notes |
|---|---|---|
| `id` | uuid PK | |
| `tenant_id` | uuid FK tenants, indexed | tenant scope |
| `lead_id` | uuid FK leads, indexed | owning lead |
| `conversation_id` | uuid, indexed, nullable | chat session |
| `currency` | str(10) | copied from config at compute time |
| `line_items` | JSON | `[{label, detail, amount}]` (amount = integer minor-unit or whole NGN) |
| `total` | Integer | exact total |
| `status` | str enum `draft`\|`sent` | lowercase (mirrors LeadStatus); `draft` in this slice |
| `created_at` | timestamptz | |

Add `Lead.quote_id` (nullable uuid, mirrors `meeting_id`) → set to the latest quote. Additive migration only (new table + `quote_id` column).

**Money:** amounts are stored/computed as **integers** (whole NGN) to avoid float drift; the engine uses integer/Decimal arithmetic.

---

## 3. Pricing config

Extend `TenantSalesAgentConfig`:
- `pricing_tiers` (already exists, JSON list, default `[]`) — **defined dict schema** per tier: `{"name": str, "base_price": int, "min_users": int, "max_users": int, "per_seat_overage_price": int}`.
- Add `annual_discount_pct` (Integer, default `0`).
- `currency` already present (default NGN), `quotation_enabled` already present.

Defaults: `pricing_tiers=[]`, `annual_discount_pct=0`. The dashboard editor (paired FE) populates these.

---

## 4. Pricing engine (pure)

`app/sales/pricing.py` → `compute_quote(pricing_tiers, annual_discount_pct, currency, *, team_size, plan_interest) -> dict | None`:
- **Tier match:** if `plan_interest` matches a tier `name` (case-insensitive), use it; else the tier whose `[min_users, max_users]` contains `team_size`; else (no match / empty tiers) → return `None` (cannot quote).
- **Compute:** `base = tier["base_price"]`; `overage_seats = max(0, team_size - tier["max_users"])`; `overage = overage_seats * tier["per_seat_overage_price"]`; `subtotal = base + overage`; `discount = subtotal * annual_discount_pct // 100`; `total = subtotal - discount`.
- **Line items** (only the rows that apply): base (`label=tier name`, `detail="{min}–{max} users"`, `amount=base`); extra seats if `overage_seats>0` (`label="Extra seats (×{n})"`, `detail="₦{rate} / seat"`, `amount=overage`); annual discount if `annual_discount_pct>0` (`label="Annual discount"`, `detail="−{pct}%"`, `amount=-discount`).
- Returns `{currency, line_items, total}`. Pure (no DB), unit-tested. Integer math throughout.

---

## 5. Backend flow

- **Piggyback:** in `app/sales/intent.py` add `offer_quote: bool` to `SALES_RESPONSE_SCHEMA.signals` (properties + `required`, keep `additionalProperties:false`), and `plan_interest: str|null` to `captured` (properties + `required`). Append a directive line: gather team size + which plan they want, then set `offer_quote=true` when ready to quote.
- **Orchestration** in `app/api/v1/endpoints/chat.py`, alongside the Slice 3 `offer_meeting` block (additive, support path untouched): when `_sales_mode and _sales_meta is not None`, if `bool(signals.get("offer_quote")) and config.quotation_enabled` and `team_size` is known (from `captured`/lead) → call `compute_quote(...)`; if it returns a quote, persist a `Quote` (status `draft`), set `lead.quote_id`, and set `_message_meta["quote"] = {currency, line_items, total}`. Server-authoritative gate — no `meta.quote` when `quotation_enabled` is false or pricing unconfigured (`compute_quote` → None).
- A `QuoteService` (`app/services/quote_service.py`) handles persistence (`create_quote(tenant_id, lead, *, currency, line_items, total)` → links `lead.quote_id`). `team_size` parsing: reuse the lead's `team_size` (string) → int extraction; if unparseable, no quote.
- On any failure → plain reply, no quote mutation (same degradation as Slices 2–3).

---

## 6. Response contract

Chat `meta` gains optional `quote`:
```
"quote": { "currency": "NGN", "line_items": [{"label","detail","amount"}], "total": int } | absent
```
Present only the turn a quote is generated. Intent/signals stay internal.

---

## 7. Safety & invariants

- Deterministic integer money math; no floats.
- Quote mutation gated by `quotation_enabled` AND `offer_quote` signal AND parseable `team_size` AND a matching tier; server-side gate authoritative.
- Tenant-scoped throughout (quote carries `tenant_id`).
- `SUPPORT`/non-quote turns and quotation-off path: existing behavior unchanged.
- No PDF/email (Slice 5). `Quote.status` stays `draft`.
- Idempotency: re-computing for the same conversation updates/links the latest quote (one active quote per lead is sufficient; a new turn may supersede).

---

## 8. Testing (pytest)

- **Pricing engine** (`tests/sales/test_pricing.py`): tier match by plan name; fallback by team_size range; base-only (no overage, no discount); overage applied; discount applied; both; empty tiers → None; no-match → None; integer exactness (no float).
- **Quote orchestration** (`tests/api/v1/test_chat_quote.py`): sales-on + `offer_quote` + `quotation_enabled` + team_size → `meta.quote` present, `Quote` persisted, `lead.quote_id` set; quotation **disabled** → no `meta.quote` even if signal true (gate); empty pricing → no quote; unparseable team_size → no quote; `SUPPORT` → no quote; cross-tenant isolation; **Slice 2 + Slice 3 regression green** (lead-capture + offer_meeting unaffected).
- Green `poetry run pytest`; full suite no regression.

---

## 9. Files

**New:** `app/db/models/quote.py`, Alembic migration (quotes table + `lead.quote_id`), `app/sales/pricing.py`, `app/services/quote_service.py`, `app/schemas/quote.py` (or extend conversation meta doc), `tests/sales/test_pricing.py`, `tests/api/v1/test_chat_quote.py`.
**Modify:** `app/sales/intent.py` (`offer_quote` + `plan_interest` + directive), `app/api/v1/endpoints/chat.py` (quote orchestration + `meta.quote`), `app/db/models/sales_agent_config.py` (`annual_discount_pct`; document `pricing_tiers` schema), `app/db/models/lead.py` (`quote_id`), `app/schemas/conversation.py` (document `meta.quote`).

---

## 10. Acceptance (PRD §2.3 / §3.5)

1. With quotation enabled + pricing configured, a visitor giving team size + plan triggers an exact in-chat quote (`meta.quote`), persisted and linked to the lead.
2. Pricing math is deterministic and exact (engine unit-tested; integer arithmetic).
3. Quotation disabled or pricing unconfigured → no quote, existing behavior unchanged (gate authoritative).
4. No PDF/email this slice. Quote logged for the leads dashboard (`Lead.quote_id`).
5. Slices 2–3 behavior unaffected; all new + existing tests green.
