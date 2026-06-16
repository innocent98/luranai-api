"""Unit tests for the pure success_condition evaluator."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.schemas.automation import TenantActionCreate, TenantActionResponse
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
    cond = {
        "all": [
            {"path": "data.status", "op": "equals", "value": "confirmed"},
            {"path": "data.ok", "op": "truthy"},
        ]
    }
    assert (
        evaluate_success_condition(cond, 200, {"data": {"status": "confirmed", "ok": True}}) is True
    )
    assert (
        evaluate_success_condition(cond, 200, {"data": {"status": "pending", "ok": True}}) is False
    )
    assert (
        evaluate_success_condition(cond, 200, {"data": {"status": "confirmed", "ok": 0}}) is False
    )


def test_op_in_not_equals_exists():
    assert (
        evaluate_success_condition(
            {"all": [{"path": "code", "op": "in", "value": ["A", "B"]}]}, 200, {"code": "B"}
        )
        is True
    )
    assert (
        evaluate_success_condition(
            {"all": [{"path": "code", "op": "not_equals", "value": "X"}]}, 200, {"code": "Y"}
        )
        is True
    )
    assert (
        evaluate_success_condition({"all": [{"path": "id", "op": "exists"}]}, 200, {"id": None})
        is True
    )
    assert (
        evaluate_success_condition({"all": [{"path": "missing", "op": "exists"}]}, 200, {"id": 1})
        is False
    )


def test_present_none_distinguished_from_missing():
    # equals None matches a present-but-None value, but NOT a missing key.
    eq_none = {"all": [{"path": "x", "op": "equals", "value": None}]}
    assert evaluate_success_condition(eq_none, 200, {"x": None}) is True
    assert evaluate_success_condition(eq_none, 200, {"y": 1}) is False


def test_in_requires_list_value():
    with pytest.raises(SuccessConditionError):
        evaluate_success_condition(
            {"all": [{"path": "x", "op": "in", "value": "AB"}]}, 200, {"x": "A"}
        )


def test_missing_path_fails_value_checks():
    cond = {"all": [{"path": "data.x", "op": "equals", "value": 1}]}
    assert evaluate_success_condition(cond, 200, {"data": {}}) is False
    assert evaluate_success_condition(cond, 200, "not-a-dict-body") is False


def test_status_in_combines_with_body_all():
    cond = {"status_in": [200], "all": [{"path": "ok", "op": "truthy"}]}
    assert evaluate_success_condition(cond, 200, {"ok": True}) is True
    assert evaluate_success_condition(cond, 200, {"ok": False}) is False
    assert evaluate_success_condition(cond, 201, {"ok": True}) is False


@pytest.mark.parametrize(
    "bad",
    [
        "not-a-dict",
        {"all": "not-a-list"},
        {"all": [{"op": "equals", "value": 1}]},  # missing path
        {"all": [{"path": "x", "op": "bogus", "value": 1}]},  # unknown op
        {"status_in": "nope"},
    ],
)
def test_malformed_condition_raises(bad):
    with pytest.raises(SuccessConditionError):
        evaluate_success_condition(bad, 200, {"x": 1})


def _minimal_create(**over):
    base = {
        "name": "Cancel",
        "slug": "cancel",
        "method": "POST",
        "url_template": "https://api.example.com/x",
    }
    base.update(over)
    return base


def test_create_rejects_malformed_success_condition():
    with pytest.raises(ValidationError):
        TenantActionCreate(**_minimal_create(success_condition={"all": "not-a-list"}))


def test_create_accepts_valid_success_condition():
    model = TenantActionCreate(
        **_minimal_create(
            success_condition={"status_in": [200], "all": [{"path": "ok", "op": "truthy"}]}
        )
    )
    assert model.success_condition["status_in"] == [200]


def test_create_rejects_bad_retry_policy():
    with pytest.raises(ValidationError):
        TenantActionCreate(**_minimal_create(retry_policy={"max_attempts": 0}))
    with pytest.raises(ValidationError):
        TenantActionCreate(**_minimal_create(retry_policy={"max_attempts": "x"}))


def test_response_does_not_validate_stored_config():
    # Validators live on the write schema only. A legacy/edge row with malformed
    # config must still serialize on read (never 500 a GET/list).
    resp = TenantActionResponse(
        name="x",
        slug="x",
        url_template="https://e.com",
        success_condition={"all": "not-a-list"},  # malformed
        retry_policy={"max_attempts": 0},  # malformed
        id=uuid4(),
        tenant_id=uuid4(),
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    assert resp.success_condition == {"all": "not-a-list"}
    assert resp.retry_policy == {"max_attempts": 0}
