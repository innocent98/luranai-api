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
async def test_attempt_request_oversized_body_is_a_non_retryable_error(monkeypatch):
    import httpx

    svc = _svc()
    action = SimpleNamespace(timeout_seconds=5, id="a1")
    req = httpx.Request("GET", "https://api.example.com/x")

    async def fake_send(self, request, **kw):
        return httpx.Response(
            200, headers={"content-length": str(50 * 1024 * 1024)}, content=b"x", request=request
        )

    monkeypatch.setattr(httpx.AsyncClient, "send", fake_send)
    outcome = await svc._attempt_request(action, req)
    assert outcome.kind == "error"
    assert not svc._outcome_retryable(outcome)


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
    svc._finalize_execution(
        ok_exec, action, _AttemptOutcome(kind="response", status_code=200, body={"x": 1})
    )
    assert ok_exec.status == ActionExecutionStatus.SUCCEEDED
    assert ok_exec.normalized_output["success"] is True

    action2 = SimpleNamespace(success_condition={"all": [{"path": "ok", "op": "truthy"}]})
    bad_exec = SimpleNamespace(meta={})
    svc._finalize_execution(
        bad_exec, action2, _AttemptOutcome(kind="response", status_code=200, body={"ok": False})
    )
    assert bad_exec.status == ActionExecutionStatus.FAILED
    assert bad_exec.error_code == "SUCCESS_CONDITION_FAILED"

    # Non-2xx response -> PROVIDER_ERROR (distinct from the 2xx-condition-fail case).
    err_exec = SimpleNamespace(meta={})
    svc._finalize_execution(
        err_exec, action, _AttemptOutcome(kind="response", status_code=404, body={"e": 1})
    )
    assert err_exec.status == ActionExecutionStatus.FAILED
    assert err_exec.error_code == "PROVIDER_ERROR"
    assert err_exec.normalized_output["success"] is False


def test_finalize_timeout_and_invalid_condition():
    from app.db.models.automation import ActionExecutionStatus
    from app.services.automation_service import _AttemptOutcome

    svc = _svc()
    t_exec = SimpleNamespace(meta={})
    svc._finalize_execution(
        t_exec,
        SimpleNamespace(success_condition=None),
        _AttemptOutcome(kind="timeout", error="too slow", latency_ms=10),
    )
    assert t_exec.status == ActionExecutionStatus.TIMED_OUT
    assert t_exec.error_code == "ACTION_TIMEOUT"

    bad_cond = SimpleNamespace(success_condition={"all": "not-a-list"})
    i_exec = SimpleNamespace(meta={})
    svc._finalize_execution(
        i_exec, bad_cond, _AttemptOutcome(kind="response", status_code=200, body={})
    )
    assert i_exec.status == ActionExecutionStatus.FAILED
    assert i_exec.error_code == "CONDITION_INVALID"


def _exec_test_service(monkeypatch, outcomes):
    """Service whose _attempt_request yields the given outcomes in order, with
    DB writes stubbed so execute_action can run without a real session."""
    svc = _svc()
    svc.db.add = MagicMock()
    svc.db.commit = MagicMock()
    svc.db.refresh = MagicMock()
    monkeypatch.setattr(svc, "_get_execution_limit", lambda tid: None)
    monkeypatch.setattr(svc, "_find_succeeded_by_idempotency", lambda *a, **k: None)
    # auth_type="none" is a no-op in production, but the real helper unconditionally
    # parses UUID(tenant_id) up front; bypass it so these wiring tests can use a
    # non-UUID tenant sentinel ("t"). _apply_auth_headers has its own dedicated tests.
    monkeypatch.setattr(svc, "_apply_auth_headers", lambda headers, auth_type, cfg: headers)
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
    from app.db.models.automation import TenantActionStatus

    base = {
        "id": "11111111-1111-1111-1111-111111111111",
        "status": None,
        "requires_approval": False,
        "allow_in_flows": True,
        "method": "POST",
        "url_template": "https://api.example.com/orders",
        "headers_template": {},
        "query_template": {},
        "body_template": {},
        "timeout_seconds": 5,
        "auth_type": SimpleNamespace(value="none"),
        "auth_config": {},
        "success_condition": None,
        "retry_policy": {"max_attempts": 3, "backoff_ms": 10},
    }
    base.update(over)
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

    svc, calls = _exec_test_service(
        monkeypatch, [_AttemptOutcome(kind="response", status_code=200, body={})]
    )
    prior = SimpleNamespace(
        id="prior",
        response_status_code=200,
        response_body={"done": True},
        normalized_output={"success": True, "data": {"done": True}},
    )
    monkeypatch.setattr(svc, "_find_succeeded_by_idempotency", lambda *a, **k: prior)
    execution = await svc.execute_action(
        tenant_id="t", action=_action_obj(), idempotency_key="dup-1"
    )
    assert calls["n"] == 0  # no HTTP send
    assert execution.status == ActionExecutionStatus.SUCCEEDED
    assert execution.meta.get("deduplicated_from") == "prior"


@pytest.mark.asyncio
async def test_execute_dedupe_synthesizes_normalized_output_when_prior_null(monkeypatch):
    from app.db.models.automation import ActionExecutionStatus
    from app.services.automation_service import _AttemptOutcome

    svc, _ = _exec_test_service(
        monkeypatch, [_AttemptOutcome(kind="response", status_code=200, body={})]
    )
    prior = SimpleNamespace(
        id="legacy", response_status_code=200, response_body={"done": True}, normalized_output=None
    )
    monkeypatch.setattr(svc, "_find_succeeded_by_idempotency", lambda *a, **k: prior)
    execution = await svc.execute_action(
        tenant_id="t", action=_action_obj(), idempotency_key="dup-2"
    )
    assert execution.status == ActionExecutionStatus.SUCCEEDED
    # A prior with NULL normalized_output gets a synthesized success payload so
    # downstream consumers can always read normalized_output["success"].
    assert execution.normalized_output["success"] is True
    assert execution.normalized_output["data"] == {"done": True}


@pytest.mark.asyncio
async def test_execute_forwards_idempotency_header_on_post(monkeypatch):
    from app.services.automation_service import _AttemptOutcome

    captured = {}
    svc, _ = _exec_test_service(
        monkeypatch, [_AttemptOutcome(kind="response", status_code=200, body={})]
    )

    async def capture_attempt(action, request):
        captured["idem"] = request.headers.get("idempotency-key")
        return _AttemptOutcome(kind="response", status_code=200, body={})

    monkeypatch.setattr(svc, "_attempt_request", capture_attempt)
    await svc.execute_action(
        tenant_id="t", action=_action_obj(method="POST"), idempotency_key="key-xyz"
    )
    assert captured["idem"] == "key-xyz"


@pytest.mark.asyncio
async def test_retry_execution_reuses_key(monkeypatch):
    svc = _svc()
    original = SimpleNamespace(
        id="e-orig",
        action_id="a1",
        input_payload={"x": 1},
        session_id=None,
        flow_execution_id=None,
        flow_id=None,
        node_id=None,
        trigger_source=SimpleNamespace(),
        idempotency_key="orig-key",
        meta={},
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
