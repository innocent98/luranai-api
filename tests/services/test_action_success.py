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


def test_present_none_distinguished_from_missing():
    # equals None matches a present-but-None value, but NOT a missing key.
    eq_none = {"all": [{"path": "x", "op": "equals", "value": None}]}
    assert evaluate_success_condition(eq_none, 200, {"x": None}) is True
    assert evaluate_success_condition(eq_none, 200, {"y": 1}) is False


def test_in_requires_list_value():
    with pytest.raises(SuccessConditionError):
        evaluate_success_condition({"all": [{"path": "x", "op": "in", "value": "AB"}]}, 200, {"x": "A"})


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
