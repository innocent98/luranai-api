# Automation Execution Safety Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make tenant action execution safe for high-stakes writes by honoring `success_condition`, `retry_policy`, and idempotency in `AutomationService.execute_action`.

**Architecture:** A pure `success_condition` evaluator module; pure retry helpers + an `_AttemptOutcome`-returning send helper on the service; a dedupe finder; then one integration task that rewires `execute_action` into idempotency-key → dedupe → retry-loop → success-evaluation. Re-runs reuse the key. An Alembic index supports the dedupe lookup, and schema validators reject malformed config.

**Tech Stack:** Python 3.11, FastAPI, SQLAlchemy, httpx ^0.27, Alembic, pytest + pytest-asyncio (`asyncio_mode = "auto"`), pytest-mock.

**Spec:** `docs/superpowers/specs/2026-06-16-automation-execution-safety-design.md`

**Branch:** create `feature/automation-execution-safety` off `develop` before Task 1.

**Commit convention:** Conventional Commits. **Do NOT add a Claude co-author trailer** (project rule). If GPG signing fails, retry with `git -c commit.gpgsign=false commit ...`.

---

## File Structure

| File | Responsibility | Action |
|------|----------------|--------|
| `app/services/action_success.py` | Pure `evaluate_success_condition` + `SuccessConditionError` | Create |
| `app/services/automation_service.py` | Retry helpers, `_AttemptOutcome`, `_attempt_request`, dedupe finder, `_finalize_execution`, rewired `execute_action`, key reuse in `retry_execution`/`set_execution_approval` | Modify |
| `app/schemas/automation.py` | Validators for `success_condition` / `retry_policy` | Modify |
| `alembic/versions/<rev>_action_exec_idem_index.py` | Index on `(tenant_id, idempotency_key)` | Create (scaffold via alembic) |
| `tests/services/test_action_success.py` | Evaluator unit tests | Create |
| `tests/services/test_automation_execution_safety.py` | Retry / idempotency / success-condition service tests | Create |

---

## Task 0: Branch + commit spec

- [ ] **Step 1: Branch off develop**

Run:
```bash
git checkout develop && git pull --ff-only && git checkout -b feature/automation-execution-safety
```
Expected: `Switched to a new branch 'feature/automation-execution-safety'`

- [ ] **Step 2: Commit the spec + plan**

```bash
git add docs/superpowers/specs/2026-06-16-automation-execution-safety-design.md docs/superpowers/plans/2026-06-16-automation-execution-safety.md
git -c commit.gpgsign=false commit -m "docs: spec + plan for automation execution safety (idempotency/retry/success_condition)"
```

---

## Task 1: `success_condition` evaluator (pure module)

**Files:**
- Create: `app/services/action_success.py`
- Test: `tests/services/test_action_success.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/services/test_action_success.py`:
```python
"""Unit tests for the pure success_condition evaluator."""

import pytest

from app.services.action_success import SuccessConditionError, evaluate_success_condition


def test_none_condition_is_2xx():
    assert evaluate_success_condition(None, 200, {"x": 1}) is True
    assert evaluate_success_condition(None, 201, None) is True
    assert evaluate_success_condition(None, 404, {"x": 1}) is False
    assert evaluate_success_condition(None, 500, None) is False


def test_status_in():
    cond = {"status_in": [200, 201]}
    assert evaluate_success_condition(cond, 201, {}) is True
    assert evaluate_success_condition(cond, 202, {}) is False


def test_body_equals_and_truthy():
    cond = {"all": [{"path": "data.status", "op": "equals", "value": "confirmed"},
                    {"path": "data.ok", "op": "truthy"}]}
    assert evaluate_success_condition(cond, 200, {"data": {"status": "confirmed", "ok": True}}) is True
    assert evaluate_success_condition(cond, 200, {"data": {"status": "pending", "ok": True}}) is False
    assert evaluate_success_condition(cond, 200, {"data": {"status": "confirmed", "ok": 0}}) is False


def test_op_in_not_equals_exists():
    assert evaluate_success_condition({"all": [{"path": "code", "op": "in", "value": ["A", "B"]}]}, 200, {"code": "B"}) is True
    assert evaluate_success_condition({"all": [{"path": "code", "op": "not_equals", "value": "X"}]}, 200, {"code": "Y"}) is True
    assert evaluate_success_condition({"all": [{"path": "id", "op": "exists"}]}, 200, {"id": None}) is True
    assert evaluate_success_condition({"all": [{"path": "missing", "op": "exists"}]}, 200, {"id": 1}) is False


def test_missing_path_fails_value_checks():
    cond = {"all": [{"path": "data.x", "op": "equals", "value": 1}]}
    assert evaluate_success_condition(cond, 200, {"data": {}}) is False
    assert evaluate_success_condition(cond, 200, "not-a-dict-body") is False


def test_status_in_combines_with_body_all():
    cond = {"status_in": [200], "all": [{"path": "ok", "op": "truthy"}]}
    assert evaluate_success_condition(cond, 200, {"ok": True}) is True
    assert evaluate_success_condition(cond, 200, {"ok": False}) is False
    assert evaluate_success_condition(cond, 201, {"ok": True}) is False


@pytest.mark.parametrize("bad", [
    "not-a-dict",
    {"all": "not-a-list"},
    {"all": [{"op": "equals", "value": 1}]},          # missing path
    {"all": [{"path": "x", "op": "bogus", "value": 1}]},  # unknown op
    {"status_in": "nope"},
])
def test_malformed_condition_raises(bad):
    with pytest.raises(SuccessConditionError):
        evaluate_success_condition(bad, 200, {"x": 1})
```

- [ ] **Step 2: Run to verify it fails**

Run: `poetry run pytest tests/services/test_action_success.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.action_success'`

- [ ] **Step 3: Implement the evaluator**

Create `app/services/action_success.py`:
```python
"""Pure evaluator for tenant-defined action success conditions.

A success_condition is None (=> HTTP 2xx) or a dict:
    {"status_in": [200, 201],
     "all": [{"path": "data.status", "op": "equals", "value": "confirmed"}, ...]}
Ops: equals, not_equals, in, exists, truthy.
Structurally invalid conditions raise SuccessConditionError (callers fail closed).
"""

from __future__ import annotations

from typing import Any

_VALUE_OPS = {"equals", "not_equals", "in"}
_NO_VALUE_OPS = {"exists", "truthy"}
_ALL_OPS = _VALUE_OPS | _NO_VALUE_OPS
_MISSING = object()


class SuccessConditionError(ValueError):
    """Raised when a success_condition is structurally invalid."""


def _resolve_path(body: Any, path: str) -> Any:
    current: Any = body
    for key in path.split("."):
        if isinstance(current, dict) and key in current:
            current = current[key]
        else:
            return _MISSING
    return current


def _check(check: dict, body: Any) -> bool:
    if not isinstance(check, dict):
        raise SuccessConditionError(f"Each check must be an object, got {type(check).__name__}")
    op = check.get("op")
    if op not in _ALL_OPS:
        raise SuccessConditionError(f"Unknown or missing op: {op!r}")
    path = check.get("path")
    if not isinstance(path, str) or not path:
        raise SuccessConditionError("Each check requires a non-empty 'path'")

    value_present = "value" in check
    if op in _VALUE_OPS and not value_present:
        raise SuccessConditionError(f"op '{op}' requires a 'value'")

    actual = _resolve_path(body, path)
    if op == "exists":
        return actual is not _MISSING
    if op == "truthy":
        return actual is not _MISSING and bool(actual)
    if actual is _MISSING:
        return False
    operand = check.get("value")
    if op == "equals":
        return actual == operand
    if op == "not_equals":
        return actual != operand
    if op == "in":
        if not isinstance(operand, list):
            raise SuccessConditionError("op 'in' requires a list 'value'")
        return actual in operand
    return False  # unreachable


def evaluate_success_condition(condition: dict | None, status_code: int, body: Any) -> bool:
    """Return True if the response counts as success per the condition."""
    if condition is None:
        return 200 <= status_code < 300
    if not isinstance(condition, dict):
        raise SuccessConditionError("success_condition must be an object or null")

    status_in = condition.get("status_in")
    if status_in is not None:
        if not isinstance(status_in, list) or not all(isinstance(s, int) for s in status_in):
            raise SuccessConditionError("status_in must be a list of integers")
        if status_code not in status_in:
            return False
    else:
        # No explicit status set => default to the 2xx gate.
        if not (200 <= status_code < 300):
            return False

    checks = condition.get("all")
    if checks is not None:
        if not isinstance(checks, list):
            raise SuccessConditionError("'all' must be a list of checks")
        for check in checks:
            if not _check(check, body):
                return False
    return True
```

- [ ] **Step 4: Run to verify pass**

Run: `poetry run pytest tests/services/test_action_success.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/services/action_success.py tests/services/test_action_success.py
git -c commit.gpgsign=false commit -m "feat(automation): pure success_condition evaluator"
```

---

## Task 2: retry helpers (pure)

**Files:**
- Modify: `app/services/automation_service.py`
- Test: `tests/services/test_automation_execution_safety.py` (create)

- [ ] **Step 1: Write the failing tests**

Create `tests/services/test_automation_execution_safety.py`:
```python
"""Tests for retry/idempotency/success wiring in AutomationService."""

from unittest.mock import AsyncMock

import httpx
import pytest

from app.services.automation_service import AutomationService


def _svc():
    return AutomationService(db=AsyncMock())


def test_parse_retry_after_seconds():
    assert _svc()._parse_retry_after("5") == 5
    assert _svc()._parse_retry_after(None) is None
    assert _svc()._parse_retry_after("garbage") is None


def test_is_retryable_status():
    svc = _svc()
    assert svc._is_retryable_status(500) is True
    assert svc._is_retryable_status(503) is True
    assert svc._is_retryable_status(429) is True
    assert svc._is_retryable_status(404) is False
    assert svc._is_retryable_status(200) is False


def test_retry_delay_exponential_and_capped():
    svc = _svc()
    assert svc._retry_delay_seconds(1, 100, None) == pytest.approx(0.1)
    assert svc._retry_delay_seconds(2, 100, None) == pytest.approx(0.2)
    assert svc._retry_delay_seconds(3, 100, None) == pytest.approx(0.4)
    # capped at 30s
    assert svc._retry_delay_seconds(20, 1000, None) == 30
    # retry_after wins when larger
    assert svc._retry_delay_seconds(1, 100, 7) == 7


def test_outcome_retryable():
    from app.services.automation_service import _AttemptOutcome

    svc = _svc()
    assert svc._outcome_retryable(_AttemptOutcome(kind="timeout")) is True
    assert svc._outcome_retryable(_AttemptOutcome(kind="transport_error")) is True
    assert svc._outcome_retryable(_AttemptOutcome(kind="response", status_code=503)) is True
    assert svc._outcome_retryable(_AttemptOutcome(kind="response", status_code=404)) is False
    assert svc._outcome_retryable(_AttemptOutcome(kind="error")) is False
```

- [ ] **Step 2: Run to verify it fails**

Run: `poetry run pytest tests/services/test_automation_execution_safety.py -v`
Expected: FAIL — `ImportError: cannot import name '_AttemptOutcome'` / missing methods.

- [ ] **Step 3: Add imports, the `_AttemptOutcome` dataclass, and the helpers**

In `app/services/automation_service.py`, update the imports near the top. Add these lines among the existing imports:
```python
import asyncio
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
```
Change `from uuid import UUID` to:
```python
from uuid import UUID, uuid4
```
Add this import with the other `from app.services...`/`from app...` imports:
```python
from app.services.action_success import SuccessConditionError, evaluate_success_condition
```

Add this module-level dataclass right after the imports (before `class AutomationService`):
```python
@dataclass
class _AttemptOutcome:
    """Outcome of a single HTTP attempt within execute_action."""

    kind: str  # "response" | "timeout" | "transport_error" | "error"
    status_code: int | None = None
    response_headers: dict | None = None
    body: object = None
    latency_ms: int = 0
    retry_after: int | None = None
    error: str | None = None
    meta: dict = field(default_factory=dict)


MAX_BACKOFF_SECONDS = 30
MAX_RETRY_ATTEMPTS = 5
```

Add these methods to `AutomationService` (place them near `_apply_auth_headers`):
```python
    def _parse_retry_after(self, value: str | None) -> int | None:
        if not value:
            return None
        value = value.strip()
        if value.isdigit():
            return int(value)
        try:
            when = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if when is None:
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        delta = (when - datetime.now(UTC)).total_seconds()
        return max(0, int(delta))

    def _is_retryable_status(self, status_code: int) -> bool:
        return status_code == 429 or 500 <= status_code <= 599

    def _outcome_retryable(self, outcome: "_AttemptOutcome") -> bool:
        if outcome.kind in {"timeout", "transport_error"}:
            return True
        if outcome.kind == "response" and outcome.status_code is not None:
            return self._is_retryable_status(outcome.status_code)
        return False

    def _retry_delay_seconds(
        self, attempt: int, backoff_ms: int, retry_after: int | None
    ) -> float:
        base = (backoff_ms / 1000) * (2 ** (attempt - 1))
        base = min(base, MAX_BACKOFF_SECONDS)
        if retry_after is not None:
            return float(max(base, retry_after))
        return base
```

- [ ] **Step 4: Run to verify pass**

Run: `poetry run pytest tests/services/test_automation_execution_safety.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/services/automation_service.py tests/services/test_automation_execution_safety.py
git -c commit.gpgsign=false commit -m "feat(automation): retry helpers and attempt outcome type"
```

---

## Task 3: dedupe finder

**Files:**
- Modify: `app/services/automation_service.py`
- Test: `tests/services/test_automation_execution_safety.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/services/test_automation_execution_safety.py`:
```python
from types import SimpleNamespace
from unittest.mock import MagicMock


def test_find_succeeded_by_idempotency_queries_and_returns():
    db = MagicMock()
    prior = SimpleNamespace(id="prior-1")
    db.execute.return_value.scalar_one_or_none.return_value = prior
    svc = AutomationService(db=db)

    found = svc._find_succeeded_by_idempotency(
        tenant_id="t1", idempotency_key="k1", exclude_id="cur-1"
    )
    assert found is prior
    assert db.execute.called  # a select was issued
```

- [ ] **Step 2: Run to verify it fails**

Run: `poetry run pytest tests/services/test_automation_execution_safety.py::test_find_succeeded_by_idempotency_queries_and_returns -v`
Expected: FAIL — `AttributeError: ... '_find_succeeded_by_idempotency'`

- [ ] **Step 3: Implement the finder**

Add to `AutomationService`:
```python
    def _find_succeeded_by_idempotency(
        self, tenant_id: UUID, idempotency_key: str, exclude_id: UUID
    ) -> "ActionExecution | None":
        stmt = (
            select(ActionExecution)
            .where(
                ActionExecution.tenant_id == tenant_id,
                ActionExecution.idempotency_key == idempotency_key,
                ActionExecution.status == ActionExecutionStatus.SUCCEEDED,
                ActionExecution.id != exclude_id,
            )
            .limit(1)
        )
        return self.db.execute(stmt).scalar_one_or_none()
```

- [ ] **Step 4: Run to verify pass**

Run: `poetry run pytest tests/services/test_automation_execution_safety.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/services/automation_service.py tests/services/test_automation_execution_safety.py
git -c commit.gpgsign=false commit -m "feat(automation): idempotency dedupe finder"
```

---

## Task 4: `_attempt_request` + `_finalize_execution`

**Files:**
- Modify: `app/services/automation_service.py`
- Test: `tests/services/test_automation_execution_safety.py`

- [ ] **Step 1: Write the failing tests**

Append:
```python
@pytest.mark.asyncio
async def test_attempt_request_response(monkeypatch):
    svc = _svc()
    action = SimpleNamespace(timeout_seconds=5)
    req = httpx.Request("GET", "https://api.example.com/x")

    async def fake_send(self, request, **kw):
        return httpx.Response(503, headers={"Retry-After": "3"}, json={"e": 1}, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "send", fake_send)
    outcome = await svc._attempt_request(action, req)
    assert outcome.kind == "response"
    assert outcome.status_code == 503
    assert outcome.retry_after == 3


@pytest.mark.asyncio
async def test_attempt_request_timeout(monkeypatch):
    svc = _svc()
    action = SimpleNamespace(timeout_seconds=5)
    req = httpx.Request("GET", "https://api.example.com/x")

    async def fake_send(self, request, **kw):
        raise httpx.ConnectTimeout("slow")

    monkeypatch.setattr(httpx.AsyncClient, "send", fake_send)
    outcome = await svc._attempt_request(action, req)
    assert outcome.kind == "timeout"


def test_finalize_success_and_condition_fail():
    from app.db.models.automation import ActionExecutionStatus
    from app.services.automation_service import _AttemptOutcome

    svc = _svc()
    action = SimpleNamespace(success_condition=None)

    ok_exec = SimpleNamespace(meta={})
    svc._finalize_execution(ok_exec, action, _AttemptOutcome(kind="response", status_code=200, body={"x": 1}))
    assert ok_exec.status == ActionExecutionStatus.SUCCEEDED
    assert ok_exec.normalized_output["success"] is True

    action2 = SimpleNamespace(success_condition={"all": [{"path": "ok", "op": "truthy"}]})
    bad_exec = SimpleNamespace(meta={})
    svc._finalize_execution(bad_exec, action2, _AttemptOutcome(kind="response", status_code=200, body={"ok": False}))
    assert bad_exec.status == ActionExecutionStatus.FAILED
    assert bad_exec.error_code == "SUCCESS_CONDITION_FAILED"


def test_finalize_timeout_and_invalid_condition():
    from app.db.models.automation import ActionExecutionStatus
    from app.services.automation_service import _AttemptOutcome

    svc = _svc()
    t_exec = SimpleNamespace(meta={})
    svc._finalize_execution(t_exec, SimpleNamespace(success_condition=None),
                            _AttemptOutcome(kind="timeout", error="too slow", latency_ms=10))
    assert t_exec.status == ActionExecutionStatus.TIMED_OUT
    assert t_exec.error_code == "ACTION_TIMEOUT"

    bad_cond = SimpleNamespace(success_condition={"all": "not-a-list"})
    i_exec = SimpleNamespace(meta={})
    svc._finalize_execution(i_exec, bad_cond,
                            _AttemptOutcome(kind="response", status_code=200, body={}))
    assert i_exec.status == ActionExecutionStatus.FAILED
    assert i_exec.error_code == "CONDITION_INVALID"
```

- [ ] **Step 2: Run to verify it fails**

Run: `poetry run pytest tests/services/test_automation_execution_safety.py -k "attempt_request or finalize" -v`
Expected: FAIL — missing `_attempt_request` / `_finalize_execution`.

- [ ] **Step 3: Implement the helpers**

Add to `AutomationService`:
```python
    async def _attempt_request(
        self, action: TenantAction, request: httpx.Request
    ) -> "_AttemptOutcome":
        started = perf_counter()
        try:
            async with httpx.AsyncClient(
                timeout=action.timeout_seconds, follow_redirects=False
            ) as client:
                response = await client.send(request)
            latency_ms = int((perf_counter() - started) * 1000)
            return _AttemptOutcome(
                kind="response",
                status_code=response.status_code,
                response_headers=dict(response.headers),
                body=self._parse_response_body(response),
                latency_ms=latency_ms,
                retry_after=self._parse_retry_after(response.headers.get("retry-after")),
            )
        except httpx.TimeoutException:
            return _AttemptOutcome(
                kind="timeout",
                latency_ms=int((perf_counter() - started) * 1000),
                error=f"Action timed out after {action.timeout_seconds}s",
            )
        except httpx.TransportError as exc:
            return _AttemptOutcome(
                kind="transport_error",
                latency_ms=int((perf_counter() - started) * 1000),
                error=str(exc),
            )
        except Exception as exc:  # noqa: BLE001 — unexpected, non-retryable
            logger.error(f"Action attempt failed ({action.id}): {exc}", exc_info=True)
            return _AttemptOutcome(
                kind="error",
                latency_ms=int((perf_counter() - started) * 1000),
                error=str(exc),
            )

    def _finalize_execution(
        self, execution: "ActionExecution", action: TenantAction, outcome: "_AttemptOutcome"
    ) -> None:
        execution.latency_ms = outcome.latency_ms
        if outcome.kind == "timeout":
            execution.status = ActionExecutionStatus.TIMED_OUT
            execution.error_code = "ACTION_TIMEOUT"
            execution.error_message = outcome.error
            return
        if outcome.kind in {"transport_error", "error"}:
            execution.status = ActionExecutionStatus.FAILED
            execution.error_code = "ACTION_EXECUTION_ERROR"
            execution.error_message = outcome.error
            return

        execution.response_status_code = outcome.status_code
        execution.response_headers = outcome.response_headers
        execution.response_body = outcome.body
        try:
            ok = evaluate_success_condition(action.success_condition, outcome.status_code, outcome.body)
        except SuccessConditionError as exc:
            execution.status = ActionExecutionStatus.FAILED
            execution.error_code = "CONDITION_INVALID"
            execution.error_message = str(exc)
            execution.normalized_output = {
                "status_code": outcome.status_code, "success": False, "data": outcome.body
            }
            return

        if ok:
            execution.status = ActionExecutionStatus.SUCCEEDED
            execution.normalized_output = {
                "status_code": outcome.status_code, "success": True, "data": outcome.body
            }
            return

        execution.status = ActionExecutionStatus.FAILED
        if 200 <= (outcome.status_code or 0) < 300:
            execution.error_code = "SUCCESS_CONDITION_FAILED"
            execution.error_message = "Response did not satisfy the action's success condition."
        else:
            execution.error_code = "PROVIDER_ERROR"
            execution.error_message = f"Provider returned HTTP {outcome.status_code}"
        execution.normalized_output = {
            "status_code": outcome.status_code, "success": False, "data": outcome.body
        }
```

- [ ] **Step 4: Run to verify pass**

Run: `poetry run pytest tests/services/test_automation_execution_safety.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/services/automation_service.py tests/services/test_automation_execution_safety.py
git -c commit.gpgsign=false commit -m "feat(automation): single-attempt send + outcome finalization"
```

---

## Task 5: integrate idempotency + dedupe + retry loop into `execute_action`

**Files:**
- Modify: `app/services/automation_service.py`
- Test: `tests/services/test_automation_execution_safety.py`

> Read the current `execute_action` first. You will (a) add an `idempotency_key` param, (b) insert idempotency/dedupe/header logic AFTER `headers = self._apply_auth_headers(...)`, and (c) REPLACE the single `started = perf_counter()` try/except send block with the retry loop. Do NOT change the policy/limit/approval blocks above the send.

- [ ] **Step 1: Write the failing tests**

Append:
```python
def _exec_test_service(monkeypatch, outcomes):
    """Service whose _attempt_request yields the given outcomes in order, with
    DB writes stubbed so execute_action can run without a real session."""
    from app.services.automation_service import _AttemptOutcome  # noqa: F401

    svc = _svc()
    svc.db.add = MagicMock()
    svc.db.commit = MagicMock()
    svc.db.refresh = MagicMock()
    monkeypatch.setattr(svc, "_get_execution_limit", lambda tid: None)
    monkeypatch.setattr(svc, "_find_succeeded_by_idempotency", lambda *a, **k: None)
    monkeypatch.setattr(
        "app.services.automation_service.resolve_and_validate_host",
        lambda host, allowed_hostnames=None: "93.184.216.34",
    )
    calls = {"n": 0}

    async def fake_attempt(action, request):
        o = outcomes[min(calls["n"], len(outcomes) - 1)]
        calls["n"] += 1
        return o

    monkeypatch.setattr(svc, "_attempt_request", fake_attempt)
    monkeypatch.setattr("app.services.automation_service.asyncio.sleep", AsyncMock())
    return svc, calls


def _action_obj(**over):
    base = dict(
        id="11111111-1111-1111-1111-111111111111", status=None, requires_approval=False,
        allow_in_flows=True, method="POST", url_template="https://api.example.com/orders",
        headers_template={}, query_template={}, body_template={}, timeout_seconds=5,
        auth_type=SimpleNamespace(value="none"), auth_config={},
        success_condition=None, retry_policy={"max_attempts": 3, "backoff_ms": 10},
    )
    base.update(over)
    from app.db.models.automation import TenantActionStatus
    base["status"] = TenantActionStatus.ACTIVE
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_execute_retries_then_succeeds(monkeypatch):
    from app.db.models.automation import ActionExecutionStatus
    from app.services.automation_service import _AttemptOutcome

    outcomes = [
        _AttemptOutcome(kind="response", status_code=503, body={"e": 1}),
        _AttemptOutcome(kind="response", status_code=200, body={"ok": True}),
    ]
    svc, calls = _exec_test_service(monkeypatch, outcomes)
    execution = await svc.execute_action(tenant_id="t", action=_action_obj())
    assert calls["n"] == 2
    assert execution.status == ActionExecutionStatus.SUCCEEDED
    assert execution.attempt == 2


@pytest.mark.asyncio
async def test_execute_4xx_not_retried(monkeypatch):
    from app.db.models.automation import ActionExecutionStatus
    from app.services.automation_service import _AttemptOutcome

    outcomes = [_AttemptOutcome(kind="response", status_code=404, body={"e": 1})]
    svc, calls = _exec_test_service(monkeypatch, outcomes)
    execution = await svc.execute_action(tenant_id="t", action=_action_obj())
    assert calls["n"] == 1
    assert execution.status == ActionExecutionStatus.FAILED
    assert execution.attempt == 1


@pytest.mark.asyncio
async def test_execute_dedupe_skips_send(monkeypatch):
    from app.db.models.automation import ActionExecutionStatus
    from app.services.automation_service import _AttemptOutcome

    svc, calls = _exec_test_service(monkeypatch, [_AttemptOutcome(kind="response", status_code=200, body={})])
    prior = SimpleNamespace(id="prior", response_status_code=200, response_body={"done": True},
                            normalized_output={"success": True, "data": {"done": True}})
    monkeypatch.setattr(svc, "_find_succeeded_by_idempotency", lambda *a, **k: prior)
    execution = await svc.execute_action(tenant_id="t", action=_action_obj(), idempotency_key="dup-1")
    assert calls["n"] == 0  # no HTTP send
    assert execution.status == ActionExecutionStatus.SUCCEEDED
    assert execution.meta.get("deduplicated_from") == "prior"


@pytest.mark.asyncio
async def test_execute_forwards_idempotency_header_on_post(monkeypatch):
    from app.services.automation_service import _AttemptOutcome

    captured = {}

    svc, _ = _exec_test_service(monkeypatch, [_AttemptOutcome(kind="response", status_code=200, body={})])

    async def capture_attempt(action, request):
        captured["idem"] = request.headers.get("idempotency-key")
        captured["host"] = request.headers.get("host")
        return _AttemptOutcome(kind="response", status_code=200, body={})

    monkeypatch.setattr(svc, "_attempt_request", capture_attempt)
    await svc.execute_action(tenant_id="t", action=_action_obj(method="POST"), idempotency_key="key-xyz")
    assert captured["idem"] == "key-xyz"
```

- [ ] **Step 2: Run to verify it fails**

Run: `poetry run pytest tests/services/test_automation_execution_safety.py -k execute -v`
Expected: FAIL — `execute_action` has no `idempotency_key` param / no retry loop / no dedupe.

- [ ] **Step 3: Edit `execute_action`**

3a. Add the param to the signature — change:
```python
        trigger_source: ActionTriggerSource = ActionTriggerSource.FLOW_NODE,
        bypass_approval_check: bool = False,
    ) -> ActionExecution:
```
to:
```python
        trigger_source: ActionTriggerSource = ActionTriggerSource.FLOW_NODE,
        bypass_approval_check: bool = False,
        idempotency_key: str | None = None,
    ) -> ActionExecution:
```

3b. Immediately AFTER this block (the auth headers application):
```python
        headers = self._apply_auth_headers(
            headers, action.auth_type.value, auth_config_with_context
        )
```
INSERT:
```python
        # Idempotency: one stable key per logical request, reused across retries
        # and approval/manual re-runs.
        idem_key = idempotency_key or uuid4().hex
        execution.idempotency_key = idem_key

        prior = self._find_succeeded_by_idempotency(
            tenant_id, idem_key, exclude_id=execution.id
        )
        if prior is not None:
            execution.status = ActionExecutionStatus.SUCCEEDED
            execution.response_status_code = prior.response_status_code
            execution.response_body = prior.response_body
            execution.normalized_output = prior.normalized_output
            execution.meta = {**(execution.meta or {}), "deduplicated_from": str(prior.id)}
            self.db.commit()
            self.db.refresh(execution)
            return execution

        if method in {"POST", "PUT", "PATCH", "DELETE"} and not any(
            k.lower() == "idempotency-key" for k in headers
        ):
            headers["Idempotency-Key"] = idem_key
```

3c. The existing `execution.status = ActionExecutionStatus.RUNNING` / `resolved_request` / `self.db.commit()` block stays as-is (it sits ABOVE `started = perf_counter()`, and because step 3b added the `Idempotency-Key` to `headers` before it, the redacted `resolved_request` now records it). REPLACE only the single-send block — from `started = perf_counter()` through the end of the `except Exception as e:` handler that returns the execution — with the retry loop (do NOT re-emit the RUNNING/resolved_request lines):
```python
        retry_policy = action.retry_policy if isinstance(action.retry_policy, dict) else {}
        max_attempts = max(1, min(int(retry_policy.get("max_attempts", 1) or 1), MAX_RETRY_ATTEMPTS))
        backoff_ms = int(retry_policy.get("backoff_ms", 0) or 0)

        attempts_meta: list[dict] = []
        outcome: _AttemptOutcome | None = None
        for attempt in range(1, max_attempts + 1):
            request = self._build_pinned_request(
                method=method, url=url, headers=headers, query=query, body=body, safe_ip=safe_ip
            )
            outcome = await self._attempt_request(action, request)
            step = {"n": attempt, "kind": outcome.kind, "status": outcome.status_code}
            if self._outcome_retryable(outcome) and attempt < max_attempts:
                delay = self._retry_delay_seconds(attempt, backoff_ms, outcome.retry_after)
                step["delay_s"] = delay
                attempts_meta.append(step)
                await asyncio.sleep(delay)
                continue
            attempts_meta.append(step)
            break

        execution.attempt = len(attempts_meta)
        execution.meta = {**(execution.meta or {}), "attempts": attempts_meta}
        self._finalize_execution(execution, action, outcome)
        self.db.commit()
        self.db.refresh(execution)
        return execution
```

- [ ] **Step 4: Run the new + existing automation tests**

Run: `poetry run pytest tests/services/test_automation_execution_safety.py tests/services/test_automation_ssrf_pinning.py -v && poetry run pytest tests/ -k automation -v`
Expected: PASS (live e2e needing network/DB may skip/error on missing Postgres — note it).

- [ ] **Step 5: Commit**

```bash
git add app/services/automation_service.py tests/services/test_automation_execution_safety.py
git -c commit.gpgsign=false commit -m "feat(automation): wire idempotency, dedupe, retry, and success_condition into execute_action"
```

---

## Task 6: reuse idempotency key on re-runs

**Files:**
- Modify: `app/services/automation_service.py` (`retry_execution`, `set_execution_approval`)
- Test: `tests/services/test_automation_execution_safety.py`

- [ ] **Step 1: Write the failing test**

Append:
```python
@pytest.mark.asyncio
async def test_retry_execution_reuses_key(monkeypatch):
    svc = _svc()
    original = SimpleNamespace(
        id="e-orig", action_id="a1", input_payload={"x": 1}, session_id=None,
        flow_execution_id=None, flow_id=None, node_id=None,
        trigger_source=SimpleNamespace(), idempotency_key="orig-key", meta={},
    )
    monkeypatch.setattr(svc, "get_execution", lambda tenant_id, execution_id: original)
    monkeypatch.setattr(svc, "get_action", lambda tenant_id, action_id: SimpleNamespace(id="a1"))
    passed = {}

    async def fake_execute(**kwargs):
        passed.update(kwargs)
        return SimpleNamespace(meta={})

    monkeypatch.setattr(svc, "execute_action", fake_execute)
    svc.db.commit = MagicMock()
    svc.db.refresh = MagicMock()
    await svc.retry_execution(tenant_id="t", execution_id="e-orig")
    assert passed.get("idempotency_key") == "orig-key"
```

- [ ] **Step 2: Run to verify it fails**

Run: `poetry run pytest tests/services/test_automation_execution_safety.py::test_retry_execution_reuses_key -v`
Expected: FAIL — `idempotency_key` not forwarded.

- [ ] **Step 3: Forward the key in both re-run paths**

In `retry_execution`, the `await self.execute_action(...)` call passes `trigger_source=execution.trigger_source`. Add `idempotency_key=execution.idempotency_key,` to that call's kwargs.

In `set_execution_approval`, the `await self.execute_action(...)` call (the approved re-run, with `bypass_approval_check=True`) — add `idempotency_key=execution.idempotency_key,` to that call's kwargs.

- [ ] **Step 4: Run to verify pass**

Run: `poetry run pytest tests/services/test_automation_execution_safety.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/services/automation_service.py tests/services/test_automation_execution_safety.py
git -c commit.gpgsign=false commit -m "feat(automation): reuse idempotency key on manual retry and approval re-run"
```

---

## Task 7: Alembic migration — idempotency index

**Files:**
- Create: `alembic/versions/<rev>_action_exec_idem_index.py` (scaffold via alembic)

- [ ] **Step 1: Scaffold the migration (auto-links to current head)**

Run: `poetry run alembic revision -m "add action_executions tenant idempotency index"`
Expected: prints `Generating .../alembic/versions/<rev>_add_action_executions_tenant_idempotency_index.py ... done`. Note the created file path.

- [ ] **Step 2: Fill in upgrade/downgrade**

Edit the generated file so `upgrade()` and `downgrade()` are exactly:
```python
def upgrade() -> None:
    op.create_index(
        "ix_action_executions_tenant_idem",
        "action_executions",
        ["tenant_id", "idempotency_key"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_action_executions_tenant_idem", table_name="action_executions")
```
Leave the generated `revision`/`down_revision` lines untouched.

- [ ] **Step 3: Verify it applies and reverses (requires a DB)**

Run: `poetry run alembic upgrade head && poetry run alembic downgrade -1 && poetry run alembic upgrade head`
Expected: no errors. If no Postgres is available in this environment, SKIP this step and note that CI/DB-ops must verify; still confirm the file imports cleanly with `poetry run python -c "import importlib,glob,os; f=sorted(glob.glob('alembic/versions/*idempotency*'))[-1]; print('ok', os.path.basename(f))"`.

- [ ] **Step 4: Commit**

```bash
git add alembic/versions/
git -c commit.gpgsign=false commit -m "feat(automation): index action_executions on (tenant_id, idempotency_key)"
```

---

## Task 8: schema validation for `success_condition` / `retry_policy`

**Files:**
- Modify: `app/schemas/automation.py`
- Test: `tests/services/test_action_success.py` (validator unit tests live with the success tests)

> Read `app/schemas/automation.py` first. It defines `TenantActionCreate` and `TenantActionUpdate` (Pydantic v2 models) with `success_condition: dict | None` and `retry_policy: dict ...`. Add field validators that reuse the evaluator's structural checks.

- [ ] **Step 1: Write the failing tests**

Append to `tests/services/test_action_success.py`:
```python
from pydantic import ValidationError

from app.schemas.automation import TenantActionCreate


def _minimal_create(**over):
    base = dict(
        name="Cancel", slug="cancel", method="POST",
        url_template="https://api.example.com/x",
    )
    base.update(over)
    return base


def test_create_rejects_malformed_success_condition():
    with pytest.raises(ValidationError):
        TenantActionCreate(**_minimal_create(success_condition={"all": "not-a-list"}))


def test_create_accepts_valid_success_condition():
    model = TenantActionCreate(**_minimal_create(
        success_condition={"status_in": [200], "all": [{"path": "ok", "op": "truthy"}]}
    ))
    assert model.success_condition["status_in"] == [200]


def test_create_rejects_bad_retry_policy():
    with pytest.raises(ValidationError):
        TenantActionCreate(**_minimal_create(retry_policy={"max_attempts": 0}))
    with pytest.raises(ValidationError):
        TenantActionCreate(**_minimal_create(retry_policy={"max_attempts": "x"}))
```
(If `TenantActionCreate` requires more mandatory fields than shown, read the model and extend `_minimal_create` with the real required fields — do not remove the assertions.)

- [ ] **Step 2: Run to verify it fails**

Run: `poetry run pytest tests/services/test_action_success.py -k "create" -v`
Expected: FAIL — no validation yet (models accept the bad input).

- [ ] **Step 3: Add validators**

In `app/schemas/automation.py`, add the import:
```python
from pydantic import field_validator
```
(if `field_validator` is not already imported), and add these validators to BOTH `TenantActionCreate` and `TenantActionUpdate` (only validate when the value is not None):
```python
    @field_validator("success_condition")
    @classmethod
    def _validate_success_condition(cls, v):
        if v is None:
            return v
        from app.services.action_success import SuccessConditionError, evaluate_success_condition

        try:
            # Structural validation only — exercise both branches with a probe body.
            evaluate_success_condition(v, 200, {})
        except SuccessConditionError as exc:
            raise ValueError(str(exc)) from exc
        return v

    @field_validator("retry_policy")
    @classmethod
    def _validate_retry_policy(cls, v):
        if v is None:
            return v
        if not isinstance(v, dict):
            raise ValueError("retry_policy must be an object")
        max_attempts = v.get("max_attempts", 1)
        backoff_ms = v.get("backoff_ms", 0)
        if not isinstance(max_attempts, int) or max_attempts < 1:
            raise ValueError("retry_policy.max_attempts must be an integer >= 1")
        if not isinstance(backoff_ms, int) or backoff_ms < 0:
            raise ValueError("retry_policy.backoff_ms must be an integer >= 0")
        return v
```
(If `retry_policy` on `TenantActionUpdate` is typed `dict | None` it already accepts None; on `TenantActionCreate` it has a default factory — the `v is None` guard keeps both safe.)

- [ ] **Step 4: Run to verify pass**

Run: `poetry run pytest tests/services/test_action_success.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/schemas/automation.py tests/services/test_action_success.py
git -c commit.gpgsign=false commit -m "feat(automation): validate success_condition and retry_policy on action config"
```

---

## Task 9: full regression + lint + type check

- [ ] **Step 1: Targeted + unit suites**

Run: `poetry run pytest tests/services/test_action_success.py tests/services/test_automation_execution_safety.py tests/services/test_automation_ssrf_pinning.py tests/unit -q`
Expected: PASS.

- [ ] **Step 2: Lint + format + type**

Run:
```bash
poetry run ruff check app tests
poetry run black --check app/services/action_success.py app/services/automation_service.py app/schemas/automation.py tests/services/test_action_success.py tests/services/test_automation_execution_safety.py
poetry run mypy app/services/automation_service.py app/services/action_success.py app/schemas/automation.py
```
Expected: all clean. If black would reformat, run `poetry run black <files>` and re-stage. If mypy flags an unavoidable third-party-stub issue, add a narrow `# type: ignore[<code>]` (do not silence broadly).

- [ ] **Step 3: Broader automation regression (DB permitting)**

Run: `poetry run pytest tests/ -k "automation or flow_action or chat_automation" -q`
Expected: PASS, except Postgres-connection-refused errors if no DB is available locally (environmental — note, not a regression).

- [ ] **Step 4: Commit any fixups**

```bash
git add -A
git -c commit.gpgsign=false commit -m "chore(automation): lint/type/format fixups for execution-safety"
```

---

## Post-implementation (outside this plan)
- `database-ops-engineer` reviews the migration before merge; confirm it applies on a real Postgres.
- Open a PR from `feature/automation-execution-safety` into `develop`.
- Follow-ups (separate specs): dashboard editor UI for `success_condition`/`retry_policy`; `error_mapping` wiring; client-supplied idempotency key for external double-submit dedupe. Recorded in `memory/project_action_call_review.md`.
