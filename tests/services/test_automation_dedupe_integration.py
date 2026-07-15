"""Postgres-backed integration test for idempotency dedupe.

Exercises the REAL `_find_succeeded_by_idempotency` SQL against a persisted
prior execution (the unit tests mock that finder). Uses the rollback-safe
`db_session` fixture, so nothing is persisted beyond the test.
"""

from uuid import uuid4

import pytest

from app.db.models.automation import (
    ActionAuthType,
    ActionExecution,
    ActionExecutionStatus,
    ActionTriggerSource,
    TenantAction,
    TenantActionStatus,
    TenantActionType,
)
from app.db.models.tenant import Tenant, TenantAIConfig, TenantCapabilities
from app.services.automation_service import AutomationService


def _make_tenant(db):
    tenant = Tenant(
        id=uuid4(),
        name=f"dedupe_{uuid4().hex[:8]}",
        display_name="Dedupe Tenant",
        api_key_hash="test_hash",
    )
    db.add(tenant)
    db.add(
        TenantAIConfig(
            id=uuid4(),
            tenant_id=tenant.id,
            brand_voice={},
            escalation_keywords=[],
            max_response_tokens=250,
            grounding_requirement=True,
        )
    )
    db.add(
        TenantCapabilities(
            id=uuid4(),
            tenant_id=tenant.id,
            max_automation_actions=25,
            max_automation_executions_per_month=1000,
        )
    )
    db.commit()
    db.refresh(tenant)
    return tenant


def _make_action(db, tenant_id):
    action = TenantAction(
        id=uuid4(),
        tenant_id=tenant_id,
        name="Place order",
        slug="place_order",
        status=TenantActionStatus.ACTIVE,
        type=TenantActionType.HTTP,
        method="POST",
        url_template="https://provider.example/api/orders",
        auth_type=ActionAuthType.NONE,
        query_template={},
        body_template={},
        headers_template={},
        input_schema={},
        allow_in_flows=True,
        available_in_ai_chat=False,
    )
    db.add(action)
    db.commit()
    db.refresh(action)
    return action


@pytest.mark.asyncio
async def test_dedupe_finds_real_prior_execution_and_skips_send(db_session, monkeypatch):
    svc = AutomationService(db_session)
    tenant = _make_tenant(db_session)
    action = _make_action(db_session, tenant.id)

    # Persist a prior SUCCEEDED execution with a known idempotency key.
    prior = ActionExecution(
        id=uuid4(),
        tenant_id=tenant.id,
        action_id=action.id,
        trigger_source=ActionTriggerSource.MANUAL_TEST,
        status=ActionExecutionStatus.SUCCEEDED,
        input_payload={},
        resolved_request={},
        attempt=1,
        idempotency_key="live-dedupe-key",
        response_status_code=200,
        response_body={"order_id": "ABC123"},
        normalized_output={"status_code": 200, "success": True, "data": {"order_id": "ABC123"}},
        meta={},
    )
    db_session.add(prior)
    db_session.commit()

    # SSRF resolution runs before dedupe; pin it so the test targets dedupe only.
    monkeypatch.setattr(
        "app.services.automation_service.resolve_and_validate_host",
        lambda host, allowed_hostnames=None: "93.184.216.34",
    )

    sent = {"n": 0}

    async def fail_if_called(_action, _request):
        sent["n"] += 1
        raise AssertionError("HTTP send must not happen when a prior succeeded")

    monkeypatch.setattr(svc, "_attempt_request", fail_if_called)

    result = await svc.execute_action(
        tenant_id=tenant.id,
        action=action,
        input_payload={},
        idempotency_key="live-dedupe-key",
        trigger_source=ActionTriggerSource.MANUAL_TEST,
    )

    # The real SQL found the prior row; no HTTP call was made; result is the copy.
    assert sent["n"] == 0
    assert result.status == ActionExecutionStatus.SUCCEEDED
    assert result.meta.get("deduplicated_from") == str(prior.id)
    assert result.normalized_output == prior.normalized_output
    assert result.id != prior.id  # a distinct execution row recorded the dedupe


@pytest.mark.asyncio
async def test_no_dedupe_when_prior_only_failed(db_session, monkeypatch):
    # A prior FAILED execution with the same key must NOT dedupe — the send proceeds.
    svc = AutomationService(db_session)
    tenant = _make_tenant(db_session)
    action = _make_action(db_session, tenant.id)

    failed_prior = ActionExecution(
        id=uuid4(),
        tenant_id=tenant.id,
        action_id=action.id,
        trigger_source=ActionTriggerSource.MANUAL_TEST,
        status=ActionExecutionStatus.FAILED,
        input_payload={},
        resolved_request={},
        attempt=1,
        idempotency_key="failed-key",
        meta={},
    )
    db_session.add(failed_prior)
    db_session.commit()

    monkeypatch.setattr(
        "app.services.automation_service.resolve_and_validate_host",
        lambda host, allowed_hostnames=None: "93.184.216.34",
    )

    from app.services.automation_service import _AttemptOutcome

    sent = {"n": 0}

    async def ok_attempt(_action, _request):
        sent["n"] += 1
        return _AttemptOutcome(kind="response", status_code=200, body={"ok": True})

    monkeypatch.setattr(svc, "_attempt_request", ok_attempt)

    result = await svc.execute_action(
        tenant_id=tenant.id,
        action=action,
        input_payload={},
        idempotency_key="failed-key",
        trigger_source=ActionTriggerSource.MANUAL_TEST,
    )

    assert sent["n"] == 1  # a prior FAILED row does not dedupe; the send happened
    assert result.status == ActionExecutionStatus.SUCCEEDED
    assert result.meta.get("deduplicated_from") is None
