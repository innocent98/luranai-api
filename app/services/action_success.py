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
