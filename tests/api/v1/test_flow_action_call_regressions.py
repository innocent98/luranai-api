"""Focused regression tests for flow automation action behavior."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db.models.automation import (
    ActionAuthType,
    ActionExecutionStatus,
    ActionTriggerSource,
    TenantAction,
    TenantActionStatus,
    TenantActionType,
)
from app.db.models.conversation import ConversationSession, SessionStatus
from app.db.models.flow import Flow, FlowStatus, FlowTriggerType
from app.db.models.tenant import Tenant, TenantAIConfig, TenantCapabilities
from app.services.chat_automation_orchestrator import ChatAutomationOrchestrator
from app.services.automation_service import AutomationService


def _create_tenant_base(db: Session, *, name_prefix: str) -> Tenant:
    tenant = Tenant(
        id=uuid4(),
        name=f"{name_prefix}_{uuid4().hex[:8]}",
        display_name=f"{name_prefix.title()} Tenant",
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
            can_create_tickets=True,
            can_access_analytics=True,
            can_upload_knowledge=True,
            max_conversations_per_month=1000,
        )
    )
    db.commit()
    db.refresh(tenant)
    return tenant


def _add_action(
    db: Session,
    *,
    tenant_id: UUID,
    name: str,
    slug: str,
    available_in_ai_chat: bool = False,
    allow_in_flows: bool = True,
    query_template: dict | None = None,
    input_schema: dict | None = None,
    description: str | None = None,
) -> TenantAction:
    action = TenantAction(
        id=uuid4(),
        tenant_id=tenant_id,
        name=name,
        slug=slug,
        description=description,
        status=TenantActionStatus.ACTIVE,
        type=TenantActionType.HTTP,
        method="GET",
        url_template="https://example.test/api",
        auth_type=ActionAuthType.NONE,
        query_template=query_template or {},
        body_template={},
        headers_template={},
        input_schema=input_schema or {},
        allow_in_flows=allow_in_flows,
        available_in_ai_chat=available_in_ai_chat,
    )
    db.add(action)
    db.commit()
    db.refresh(action)
    return action


def _create_manual_flow(db: Session, *, tenant_id: UUID, name: str, nodes: dict) -> Flow:
    flow = Flow(
        id=uuid4(),
        tenant_id=tenant_id,
        name=name,
        description="Regression flow",
        status=FlowStatus.ACTIVE,
        is_active=True,
        trigger_type=FlowTriggerType.MANUAL,
        trigger_config={"menu_label": name, "exclude_from_router": False},
        nodes=nodes,
        priority=100,
        version=1,
    )
    db.add(flow)
    db.commit()
    db.refresh(flow)
    return flow


def _start_flow(client: TestClient, tenant_id: UUID, flow_id: UUID, user_identifier: str):
    response = client.post(
        "/api/v1/chat/message",
        json={
            "tenant_id": str(tenant_id),
            "message": f"__flow_start__:{flow_id}",
            "user_identifier": user_identifier,
        },
    )
    assert response.status_code == 200
    return response.json()


def _post_flow_input(client: TestClient, session_id: str, user_identifier: str, message: str):
    response = client.post(
        "/api/v1/chat/message",
        json={
            "session_id": session_id,
            "message": message,
            "user_identifier": user_identifier,
        },
    )
    assert response.status_code == 200
    return response.json()


def test_action_call_failure_routes_to_failure_end_message(
    client: TestClient, db: Session, mocker
):
    tenant = _create_tenant_base(db, name_prefix="flow_failure")
    action = _add_action(
        db,
        tenant_id=tenant.id,
        name="Track Order Status",
        slug="track_order_status",
    )
    flow = _create_manual_flow(
        db,
        tenant_id=tenant.id,
        name="Failure Flow",
        nodes={
            "start_node_id": "ask_order",
            "nodes": [
                {
                    "id": "ask_order",
                    "type": "text_input",
                    "config": {
                        "prompt": "Please share your order number.",
                        "save_to": "customer_order_number",
                        "next_node_id": "call_action",
                    },
                },
                {
                    "id": "call_action",
                    "type": "action_call",
                    "config": {
                        "action_id": str(action.id),
                        "input_map": {"order_reference": "{{customer_order_number}}"},
                        "output_save_to": "order_lookup_result",
                        "output_map": {"final_error_detail": "$.response.detail"},
                        "on_success_next_node_id": "done",
                        "on_failure_next_node_id": "failure",
                    },
                },
                {
                    "id": "done",
                    "type": "end",
                    "config": {"message": "Success should not be shown."},
                },
                {
                    "id": "failure",
                    "type": "end",
                    "config": {
                        "message": "We couldn't complete the order lookup for {{customer_order_number}}. {{final_error_detail}}."
                    },
                },
            ],
            "edges": [],
        },
    )

    mocker.patch.object(
        AutomationService,
        "execute_action",
        new=mocker.AsyncMock(
            return_value=SimpleNamespace(
                id=uuid4(),
                status=ActionExecutionStatus.FAILED,
                normalized_output={"status_code": 404, "success": False, "data": {"detail": "Order not found"}},
                response_body={"detail": "Order not found"},
                error_message="Provider returned HTTP 404",
                response_status_code=404,
            )
        ),
    )

    user_identifier = "flow-failure-regression"
    start = _start_flow(client, tenant.id, flow.id, user_identifier)
    assert "order" in start["message"]["content"].lower()
    done = _post_flow_input(client, start["session_id"], user_identifier, "1234")
    assert "1234" in done["message"]["content"]
    assert "order not found" in done["message"]["content"].lower()


def test_action_call_success_interpolates_end_message(
    client: TestClient, db: Session, mocker
):
    tenant = _create_tenant_base(db, name_prefix="flow_success")
    action = _add_action(
        db,
        tenant_id=tenant.id,
        name="Track Order Status",
        slug="track_order_status",
    )
    flow = _create_manual_flow(
        db,
        tenant_id=tenant.id,
        name="Success Flow",
        nodes={
            "start_node_id": "ask_order",
            "nodes": [
                {
                    "id": "ask_order",
                    "type": "text_input",
                    "config": {
                        "prompt": "Please share your order number.",
                        "save_to": "customer_order_number",
                        "next_node_id": "call_action",
                    },
                },
                {
                    "id": "call_action",
                    "type": "action_call",
                    "config": {
                        "action_id": str(action.id),
                        "input_map": {"order_reference": "{{customer_order_number}}"},
                        "output_save_to": "order_lookup_result",
                        "output_map": {
                            "final_order_status": "$.normalized.data.status",
                            "final_eta_date": "$.normalized.data.eta_date",
                        },
                        "on_success_next_node_id": "done",
                        "on_failure_next_node_id": "failure",
                    },
                },
                {
                    "id": "done",
                    "type": "end",
                    "config": {
                        "message": "Your order is {{final_order_status}} and arrives on {{final_eta_date}}."
                    },
                },
                {
                    "id": "failure",
                    "type": "end",
                    "config": {"message": "Failure should not be shown."},
                },
            ],
            "edges": [],
        },
    )

    mocker.patch.object(
        AutomationService,
        "execute_action",
        new=mocker.AsyncMock(
            return_value=SimpleNamespace(
                id=uuid4(),
                status=ActionExecutionStatus.SUCCEEDED,
                normalized_output={
                    "status_code": 200,
                    "success": True,
                    "data": {"status": "processing", "eta_date": "2026-03-25"},
                },
                response_body={"status": "processing", "eta_date": "2026-03-25"},
                error_message=None,
                response_status_code=200,
            )
        ),
    )

    user_identifier = "flow-success-regression"
    start = _start_flow(client, tenant.id, flow.id, user_identifier)
    done = _post_flow_input(client, start["session_id"], user_identifier, "ORD1234")
    assert "processing" in done["message"]["content"].lower()
    assert "2026-03-25" in done["message"]["content"]


def test_approval_required_action_routes_to_escalation_path(
    client: TestClient, db: Session, mocker
):
    tenant = _create_tenant_base(db, name_prefix="flow_approval")
    action = _add_action(
        db,
        tenant_id=tenant.id,
        name="Unlock Suspended Account",
        slug="unlock_suspended_account",
    )
    flow = _create_manual_flow(
        db,
        tenant_id=tenant.id,
        name="Approval Flow",
        nodes={
            "start_node_id": "ask_email",
            "nodes": [
                {
                    "id": "ask_email",
                    "type": "text_input",
                    "config": {
                        "prompt": "Please share your account email.",
                        "save_to": "customer_email",
                        "next_node_id": "call_action",
                    },
                },
                {
                    "id": "call_action",
                    "type": "action_call",
                    "config": {
                        "action_id": str(action.id),
                        "input_map": {"customer_email": "{{customer_email}}"},
                        "on_approval_required_next_node_id": "approval",
                        "on_failure_next_node_id": "failure",
                    },
                },
                {
                    "id": "approval",
                    "type": "escalate",
                    "config": {
                        "reason": "Approval required for account unlock.",
                        "message": "This request needs team approval before it can continue.",
                    },
                },
                {
                    "id": "failure",
                    "type": "end",
                    "config": {"message": "Failure should not be shown."},
                },
            ],
            "edges": [],
        },
    )

    mocker.patch.object(
        AutomationService,
        "execute_action",
        new=mocker.AsyncMock(
            return_value=SimpleNamespace(
                id=uuid4(),
                status=ActionExecutionStatus.BLOCKED_POLICY,
                normalized_output=None,
                response_body=None,
                error_message="Approval required",
                error_code="ACTION_APPROVAL_REQUIRED",
                response_status_code=None,
            )
        ),
    )

    user_identifier = "flow-approval-regression"
    start = _start_flow(client, tenant.id, flow.id, user_identifier)
    done = _post_flow_input(client, start["session_id"], user_identifier, "user@example.com")
    assert done["should_escalate"] is True
    assert "approval" in done["message"]["content"].lower()


@pytest.mark.asyncio
async def test_lookup_chain_collects_lookup_input_before_target_action(db: Session, mocker):
    tenant = _create_tenant_base(db, name_prefix="lookup_chain")
    session = ConversationSession(
        id=uuid4(),
        tenant_id=tenant.id,
        user_identifier="lookup-chain-user",
        status=SessionStatus.ACTIVE,
        meta={},
    )
    db.add(session)
    db.commit()
    db.refresh(session)

    lookup_action = _add_action(
        db,
        tenant_id=tenant.id,
        name="Lookup Account By Email",
        slug="lookup_account_by_email_chat",
        available_in_ai_chat=True,
        query_template={"customer_email": "{{customer_email}}"},
        description="Lookup account by customer email",
    )
    target_action = _add_action(
        db,
        tenant_id=tenant.id,
        name="Check Account Status",
        slug="check_account_status_chat",
        available_in_ai_chat=True,
        query_template={"account_id": "{{account_id}}"},
        input_schema={
            "lookup_dependencies": {
                "account_id": {
                    "lookup_action_slug": lookup_action.slug,
                    "lookup_required_fields": ["customer_email"],
                }
            }
        },
        description="Check customer account status",
    )

    async def fake_execute_action(self, *, action, **kwargs):
        if action.id == lookup_action.id:
            return SimpleNamespace(
                id=uuid4(),
                status=ActionExecutionStatus.SUCCEEDED,
                normalized_output={
                    "status_code": 200,
                    "success": True,
                    "data": {"account_id": "acct_123"},
                },
                response_body={"account_id": "acct_123"},
                error_message=None,
                response_status_code=200,
            )
        if action.id == target_action.id:
            return SimpleNamespace(
                id=uuid4(),
                status=ActionExecutionStatus.SUCCEEDED,
                normalized_output={
                    "status_code": 200,
                    "success": True,
                    "data": {"status": "active"},
                },
                response_body={"status": "active"},
                error_message=None,
                response_status_code=200,
            )
        raise AssertionError("Unexpected action executed")

    mocker.patch.object(AutomationService, "execute_action", new=fake_execute_action)

    orchestrator = ChatAutomationOrchestrator(db)
    session_meta: dict = {}

    step1 = await orchestrator.handle_turn(
        tenant_id=tenant.id,
        session_id=session.id,
        user_message="Can you check my account status?",
        session_meta=session_meta,
        conversation_history=[],
        tenant_ai_config=None,
        company_name=tenant.display_name,
    )
    assert step1.handled is True
    assert "email" in (step1.assistant_text or "").lower()
    assert "pending_ai_action" in session_meta

    step2 = await orchestrator.handle_turn(
        tenant_id=tenant.id,
        session_id=session.id,
        user_message="user@example.com",
        session_meta=session_meta,
        conversation_history=[],
        tenant_ai_config=None,
        company_name=tenant.display_name,
    )
    assert step2.handled is True
    assert "active" in (step2.assistant_text or "").lower()
