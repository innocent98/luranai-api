# Automation execution safety: idempotency, retry, and success_condition

**Date:** 2026-06-16
**Status:** Approved (design)
**Area:** Backend — automation / `action_call` execution engine (`luran/`)
**Author:** Adebayo

## Summary

Make tenant action execution trustworthy for high-stakes, mutating writes (place order, charge, book) by wiring up three mechanisms that are already defined in the data model but ignored at execution time:

1. **`success_condition`** — evaluate a tenant-defined success rule beyond plain HTTP 2xx, fixing the "HTTP 200 with `{success:false}`" false-success.
2. **`retry_policy`** — retry transient failures (timeouts, connection errors, 5xx, 429) with exponential backoff and `Retry-After` support.
3. **Idempotency** — generate/persist an idempotency key, dedupe server-side against prior successful executions, and forward it as an `Idempotency-Key` header so retries / approval re-runs / manual retries cannot double-fire a write.

All three default to **current behavior** (no retries, 2xx = success, idempotency harmless for reads), so existing actions are unaffected until a tenant opts in.

## Background / current state (verified 2026-06-16, on `develop` post-PR #26)

- `AutomationService.execute_action` (`app/services/automation_service.py`) builds the request, SSRF-validates + pins the IP, sends once via `httpx`, and classifies success as `200 <= status < 300` → `SUCCEEDED`, else `FAILED` (`PROVIDER_ERROR`); `httpx.TimeoutException` → `TIMED_OUT`. `attempt` is hardcoded to 1.
- `TenantAction` model already has `success_condition: dict | None`, `retry_policy: dict` (default `{"max_attempts": 1, "backoff_ms": 0}`), `error_mapping: dict`. `ActionExecution` already has `idempotency_key: str | None` and `attempt: int`. None of these are read by `execute_action`.
- `retry_execution(tenant_id, execution_id)` and `set_execution_approval(...)` both call `execute_action` fresh, creating a NEW `ActionExecution` row and minting a NEW (absent) idempotency key.
- The dashboard (`luran-dashboard`) already round-trips `success_condition` (default `null`), `error_mapping` (`{}`), `retry_policy` (`{max_attempts:1, backoff_ms:0}`) on create/update and displays `idempotency_key` read-only — but has **no editor UI** for them. So the API contract carries them; only the engine ignores them.

## Decisions locked with stakeholder

- **Idempotency:** server-side dedupe AND forward an `Idempotency-Key` header.
- **Retry:** transient failures only (timeout / connection / 5xx / 429), never 4xx; exponential backoff from `backoff_ms`; honor `Retry-After`.
- **success_condition:** minimal declarative DSL (status-code set + body JSON-path checks); `null` = 2xx (backward compatible).

## Per-attempt classification (the core control flow)

For each HTTP attempt, classify the outcome in this order:

1. **Transient signal** (`httpx.TimeoutException`, `httpx.ConnectError`/`TransportError`, HTTP 5xx, HTTP 429) → **retryable**: if attempts remain, back off and retry; else final status `TIMED_OUT` (for timeout) or `FAILED` (`PROVIDER_ERROR`).
2. **Response received, evaluate `success_condition`**:
   - condition passes → `SUCCEEDED`.
   - condition fails on a 2xx (deterministic, e.g. `{success:false}`) → `FAILED` (`SUCCESS_CONDITION_FAILED`), **no retry**.
   - condition references invalid/malformed config → `FAILED` (`CONDITION_INVALID`), **no retry** (fail closed).
3. **4xx** (and any non-2xx that isn't 5xx/429) → `FAILED` (`PROVIDER_ERROR`), **no retry**.

`null` success_condition collapses step 2 to "2xx → SUCCEEDED, else FAILED" (today's behavior).

---

## Component 1 — `success_condition` evaluator

### New module: `app/services/action_success.py`
A pure, dependency-free evaluator so it is unit-testable in isolation.

```
def evaluate_success_condition(condition: dict | None, status_code: int, body: Any) -> bool
```

- `condition is None` → return `200 <= status_code < 300`.
- Supported shape:
```json
{
  "status_in": [200, 201],
  "all": [
    {"path": "data.status", "op": "equals", "value": "confirmed"},
    {"path": "data.ok", "op": "truthy"}
  ]
}
```
- `status_in` (optional): list of acceptable status codes. If present, status must be in it; if absent, default 2xx check applies.
- `all` (optional): list of body checks, ALL must pass (logical AND). Each check:
  - `path`: dotted path into the parsed JSON body (e.g. `data.status`; missing intermediate keys → value is absent).
  - `op`: one of `equals`, `not_equals`, `in`, `exists`, `truthy`.
  - `value`: comparison operand (not required for `exists`/`truthy`).
- A condition that is non-`None` but structurally invalid (not a dict, unknown op, `all` not a list, `path` missing on a non-`exists`/`truthy` check) raises `SuccessConditionError`, which `execute_action` maps to `CONDITION_INVALID` / `FAILED` (fail closed).
- `normalized_output.success` is set from this evaluator's result.

### Validation
Add a Pydantic validator in `app/schemas/automation.py` for `success_condition` on create/update so malformed conditions are rejected at config time (defense in depth), while the runtime evaluator still fails closed if a bad one slips through.

---

## Component 2 — retry with exponential backoff

### Inside `execute_action` (helpers extracted for readability)
- `_is_retryable(exc_or_status) -> bool`: True for `httpx.TimeoutException`, `httpx.ConnectError`/`httpx.TransportError`, HTTP status in 5xx, or 429. False otherwise.
- `_retry_delay_seconds(attempt, backoff_ms, retry_after) -> float`: `base = (backoff_ms / 1000) * 2 ** (attempt - 1)`, capped at `MAX_BACKOFF_SECONDS = 30`; if `retry_after` (parsed from the `Retry-After` header, integer seconds or HTTP-date) is present, use `max(base, retry_after)`.
- Loop: `max_attempts = retry_policy.get("max_attempts", 1)` (clamped to a sane ceiling, e.g. 5). For attempt in `1..max_attempts`: perform request; on retryable outcome with attempts remaining → `await asyncio.sleep(delay)` and continue; otherwise break with the final outcome.
- Persist `execution.attempt = <final attempt number>` and record per-attempt context in `execution.meta` (e.g. `{"attempts": [{"n":1,"status":503,"delay_ms":...}, ...]}`) for debuggability.
- The whole retry sequence shares ONE `ActionExecution` row and ONE idempotency key.

### Bounds
- `max_attempts` clamped to `[1, 5]`; total time also bounded by `action.timeout_seconds * max_attempts + sum(delays)` implicitly (each attempt keeps the per-request timeout).
- Retry only applies to the live send; SSRF/policy/approval/quota blocks happen before the loop and never retry.

---

## Component 3 — idempotency (dedupe + header)

### Key lifecycle
- `execute_action` accepts an optional `idempotency_key: str | None` param. If absent, generate `uuid4().hex` and set it on the `ActionExecution` row at creation.
- `retry_execution` and `set_execution_approval` pass the ORIGINAL execution's `idempotency_key` into `execute_action` so re-runs reuse it (they currently mint a new row with no key).

### Server-side dedupe (before the send, after policy checks)
- Query for a DIFFERENT `ActionExecution` with the same `tenant_id` + `idempotency_key` and `status == SUCCEEDED`.
- If found: copy `response_status_code`, `response_body`, `normalized_output` from it onto the current execution, set `status = SUCCEEDED`, `meta.deduplicated_from = <prior id>`, commit, and RETURN without making the HTTP call.
- Because each logical request gets a unique key (uuid4) reused only across its own retries/re-runs, dedupe only ever collapses re-executions of the SAME request — never two genuinely distinct customer requests with identical inputs.
- Add a DB index to support the lookup: `ix_action_executions_tenant_idem` on `(tenant_id, idempotency_key)` (partial/where-not-null acceptable) via an Alembic migration.

### Forwarded header
- For mutating methods (`POST`/`PUT`/`PATCH`/`DELETE`), inject `Idempotency-Key: <key>` into the request headers (after auth headers; do not overwrite a tenant-set header of the same name — tenant wins). GET requests do not receive it.
- The header is redacted-safe (not a secret) and recorded in `resolved_request` as today.

---

## Error handling, new codes, and backward compatibility

- New `error_code` values: `CONDITION_INVALID`, `SUCCESS_CONDITION_FAILED`. Existing `PROVIDER_ERROR`, `ACTION_TIMEOUT`, `UNSAFE_URL`, policy codes unchanged.
- `TIMED_OUT` is only final after retries are exhausted; intermediate timeouts that get retried are not the terminal status.
- Backward compatibility: `retry_policy` default `{max_attempts:1}` → exactly one attempt (today). `success_condition` null → 2xx rule (today). Idempotency key is always generated but only changes behavior on re-runs; the forwarded header is ignored by providers that don't use it. Existing tests/actions behave identically.

## Files to create / modify

| File | Responsibility | Action |
|------|----------------|--------|
| `app/services/action_success.py` | Pure `success_condition` evaluator + `SuccessConditionError` | Create |
| `app/services/automation_service.py` | Retry loop, idempotency key + dedupe + header, success evaluation in `execute_action`; reuse key in `retry_execution`/`set_execution_approval` | Modify |
| `app/schemas/automation.py` | Validate `success_condition` / `retry_policy` shape on create/update | Modify |
| `alembic/versions/<rev>_action_exec_idem_index.py` | Index on `(tenant_id, idempotency_key)` | Create |
| `tests/services/test_action_success.py` | Evaluator unit tests | Create |
| `tests/services/test_automation_execution_safety.py` | Retry / idempotency / success-condition service tests | Create |

## Testing

- **Evaluator (pure unit):** null→2xx; `status_in`; each op (`equals`/`not_equals`/`in`/`exists`/`truthy`); nested `path`; missing path; malformed condition → raises.
- **Retry (mocked transport):** 503-then-200 succeeds with `attempt==2`; timeout-then-200; 404 → no retry (`attempt==1`); 200-with-`{success:false}` under a `success_condition` → `FAILED`, no retry; `Retry-After: 2` honored (delay ≥ 2s, asserted via a patched sleep capturing the delay); `max_attempts` clamp respected.
- **Idempotency:** second execution with same key + prior SUCCEEDED → no HTTP call, `meta.deduplicated_from` set; `retry_execution` reuses the original key; approval re-run reuses the original key; GET gets no header, POST does; tenant-set `Idempotency-Key` not overwritten.
- All existing automation tests stay green.

## Delivery

- Branch `feature/automation-execution-safety` off `develop`.
- Implement with the `backend-engineer` agent, TDD, with a focused review on the dedupe path (correctness of the "same key, prior succeeded → skip call" logic).
- Alembic migration reviewed by the `database-ops-engineer` agent before merge.
- Commits follow Conventional Commits; **no Claude co-author trailer** (project rule).

## Out of scope (follow-ups)

- Dashboard editor UI for `success_condition` / `retry_policy` (they're already API-round-tripped; a UI is a separate task).
- `error_mapping` (the 4th unused field) — friendly error mapping is a separate concern.
- External rapid double-submit dedupe via a client-supplied idempotency key (this design covers retry / approval / manual re-run — the deterministic dupes; a true duplicate API submit from the caller would need a client-provided key).

## Risks

- Retry amplifies load on a struggling provider; mitigated by the `max_attempts` clamp, exponential backoff, and `Retry-After`.
- Dedupe correctness is the highest-risk piece — a wrong key scope could either fail to dedupe (duplicate write) or over-dedupe (suppress a legitimate request). Mitigated by the unique-per-request uuid4 key and explicit tests; the migration index keeps the lookup cheap.
- `Retry-After` parsing must handle both integer-seconds and HTTP-date forms; covered by tests.
