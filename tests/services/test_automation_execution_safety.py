"""Tests for retry/idempotency/success wiring in AutomationService."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.automation_service import AutomationService


def _svc():
    return AutomationService(db=AsyncMock())


def test_parse_retry_after_seconds():
    assert _svc()._parse_retry_after("5") == 5
    assert _svc()._parse_retry_after(None) is None
    assert _svc()._parse_retry_after("garbage") is None


def test_parse_retry_after_http_date():
    svc = _svc()
    # Far-future HTTP-date -> a large positive number of seconds.
    assert svc._parse_retry_after("Wed, 21 Oct 2099 07:28:00 GMT") > 0
    # Past HTTP-date -> clamped to 0 (never negative).
    assert svc._parse_retry_after("Wed, 21 Oct 1999 07:28:00 GMT") == 0


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


@pytest.mark.asyncio
async def test_attempt_request_response(monkeypatch):
    import httpx

    svc = _svc()
    action = SimpleNamespace(timeout_seconds=5, id="a1")
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
    import httpx

    svc = _svc()
    action = SimpleNamespace(timeout_seconds=5, id="a1")
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

    # Non-2xx response -> PROVIDER_ERROR (distinct from the 2xx-condition-fail case).
    err_exec = SimpleNamespace(meta={})
    svc._finalize_execution(err_exec, action, _AttemptOutcome(kind="response", status_code=404, body={"e": 1}))
    assert err_exec.status == ActionExecutionStatus.FAILED
    assert err_exec.error_code == "PROVIDER_ERROR"
    assert err_exec.normalized_output["success"] is False


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
