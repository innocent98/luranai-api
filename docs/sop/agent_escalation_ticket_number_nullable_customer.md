# SOP — Agent-app escalations: expose ticket_number, nullable customer_name

**Status:** Shipped (branch, pre-PR) · `feat/agent-escalation-ticket-number` · PR → `develop` · 2026-09-23
**Area:** `app/schemas/agent_app.py`, `app/api/v1/endpoints/agent_app.py`, `tests/agent_caller/test_agent_escalations_api.py`

---

## What shipped

`GET /agent/escalations` (list) and `GET /agent/escalations/{id}` (detail) now return the
human-readable **`ticket_number`** (e.g. `TKT-2024-0001`), and **`customer_name` is nullable** —
`null` for an unidentified public-widget visitor, the real name when the visitor has one.

The mobile app can now tell an identified customer from an anonymous one and render
`#<ticket_number>` in place of a generic label. Response-schema-only change — **no migration**.

## Why

Both endpoints previously sent `customer_name="Customer"` as a placeholder for anonymous visitors
(reusing the shared ring helper `customer_name()`), so the app could not distinguish "a customer
named Customer" from "an unidentified visitor" and had no ticket number to fall back on.

## How

- **Schema** (`app/schemas/agent_app.py` · `EscalationSummary`, inherited by `EscalationDetail`):
  added `ticket_number: str`; widened `customer_name: str` → `customer_name: str | None`.
- **Endpoint** (`app/api/v1/endpoints/agent_app.py`):
  - `_summary()` now sets `ticket_number=ticket.ticket_number` (the `ticket` was already in scope).
  - New **local** helper `_visitor_display_name_or_none(db, session)` returns `visitor.name` or
    `None` (never the "Customer" placeholder). `_summary()` uses it for `customer_name`.
  - The detail path builds on `_summary()` via `**base.model_dump()`, so both new fields flow
    through automatically — no separate wiring.
  - Dropped the now-unused `customer_name` import; added `Visitor` import.
- **Deliberately did NOT touch** the shared `customer_name()` helper in
  `app/services/agent_alert_service.py` — it still returns "Customer" and is used by the
  escalation-ring / FCM push path (`build_alert_payload`), which must keep the placeholder. The
  nullable name is resolved locally in the endpoint so the two contracts stay independent.

**Alternatives rejected:** parametrising the shared helper with a "nullable" flag — rejected
because it couples the REST list contract to the push-payload contract; a sibling local helper
keeps them from drifting into each other.

## What's involved

| File | Change |
|---|---|
| `app/schemas/agent_app.py` | `EscalationSummary`: `+ ticket_number: str`, `customer_name: str \| None` |
| `app/api/v1/endpoints/agent_app.py` | `+_visitor_display_name_or_none()`; `_summary` sets `ticket_number` + nullable name; import swap (`Visitor` in, `customer_name` out) |
| `tests/agent_caller/test_agent_escalations_api.py` | Updated list/detail assertions; +2 identified-visitor tests; `identified_visitor` fixture |

## Verification

- **Tests** (TDD — red first, then green):
  - `test_list_returns_pending_escalation` — asserts `ticket_number == ticket.ticket_number` and
    `customer_name is None` for the anonymous fixture.
  - `test_list_customer_name_is_real_name_for_identified_visitor` — visitor with `name` → real name.
  - `test_detail_includes_transcript` — extended to assert `ticket_number` + `customer_name is None`.
  - `test_detail_customer_name_is_real_name_for_identified_visitor` — detail path, identified visitor.
- `test_agent_alert_service.py::...` still asserts the push payload `customer_name == "Customer"`
  (shared helper untouched).
- **CI reproduced locally** (pinned toolchain — ruff 0.8.6, black 24.10.0):
  - `poetry run ruff check app/` → clean
  - `poetry run black --check app/` → clean
  - `poetry run pytest tests/agent_caller/ --no-cov` → **99 passed**
  - Import smoke: `python -c "import app.api.v1.endpoints.agent_app"` → OK

## Operate / roll back

Pure application change, no migration and no config. Roll back by reverting the branch commit —
both endpoints return to the "Customer" placeholder and drop `ticket_number`.

## Follow-ups

- **FE integration guide not yet captured live** for this endpoint pair — the standing
  `docs/fe-integration-guide-*.md` rule wants real captured responses, which needs the running app;
  out of scope for this narrow change. When the agent-app FE guide is authored, document that
  `customer_name` is present-but-null (not absent) for anonymous visitors and that the app should
  render `#<ticket_number>` in that case.
- Local full-suite run used the Python 3.14 venv vs CI's 3.11; all touched paths are version-neutral.
