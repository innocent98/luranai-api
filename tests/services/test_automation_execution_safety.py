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
