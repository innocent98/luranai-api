"""Focused regression tests for flow automation action behavior."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db.models.automation import (
    ActionAuthType,
    ActionExecution,
    ActionExecutionStatus,
    ActionTriggerSource,
    TenantAction,
    TenantActionStatus,
    TenantActionType,
)
from app.db.models.conversation import ConversationSession, SessionStatus
from app.db.models.flow import Flow, FlowStatus, FlowTriggerType
from app.db.models.subscription_plan import SubscriptionPlan
from app.db.models.tenant import Tenant, TenantAIConfig, TenantCapabilities
from app.services.chat_automation_orchestrator import ChatAutomationOrchestrator
from app.services.automation_service import AutomationService
from app.services.flow_service import FlowService
from app.schemas.automation import TenantActionCreate
from app.schemas.flow import FlowCreate


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
            max_automation_actions=25,
            max_automation_executions_per_month=1000,
            can_use_ai_chat_automation=True,
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


def _ensure_subscription_plan(
    db: Session,
    *,
    tier: str,
    max_flows: int | None,
    can_use_webhooks: bool,
    max_webhooks_per_flow: int | None,
) -> SubscriptionPlan:
    plan = db.query(SubscriptionPlan).filter(SubscriptionPlan.tier == tier).one_or_none()
    if plan:
        plan.max_flows = max_flows
        plan.can_use_webhooks = can_use_webhooks
        plan.max_webhooks_per_flow = max_webhooks_per_flow
        plan.is_active = True
        db.commit()
        db.refresh(plan)
        return plan

    plan = SubscriptionPlan(
        tier=tier,
        display_name=tier.title(),
        description=f"{tier.title()} test plan",
        monthly_price_kobo=100000,
        annual_price_kobo=1000000,
        can_access_analytics=True,
        max_knowledge_sources=50,
        can_use_url_sources=True,
        max_knowledge_mb=200,
        max_conversations_per_month=10000,
        max_team_members=10,
        max_response_tokens=2000,
        max_automation_actions=20,
        max_automation_executions_per_month=5000,
        can_use_ai_chat_automation=True,
        can_use_lookup_chains=True,
        max_flows=max_flows,
        max_nodes_per_flow=20,
        can_use_webhooks=can_use_webhooks,
        max_webhooks_per_flow=max_webhooks_per_flow,
        features=[],
        is_active=True,
        sort_order=1,
    )
    db.add(plan)
    db.commit()
    db.refresh(plan)
    return plan


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


def test_action_call_failure_routes_to_failure_end_message(client: TestClient, db: Session, mocker):
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
                normalized_output={
                    "status_code": 404,
                    "success": False,
                    "data": {"detail": "Order not found"},
                },
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


def test_automation_action_creation_respects_plan_limit(db: Session):
    tenant = _create_tenant_base(db, name_prefix="automation_limit")
    capabilities = (
        db.query(TenantCapabilities).filter(TenantCapabilities.tenant_id == tenant.id).one()
    )
    capabilities.max_automation_actions = 2
    db.commit()

    service = AutomationService(db)

    def build_payload(name: str, slug: str) -> TenantActionCreate:
        return TenantActionCreate(
            name=name,
            slug=slug,
            method="POST",
            url_template="https://example.test/actions",
        )

    service.create_action(tenant.id, None, build_payload("Action One", "action_one"))
    service.create_action(tenant.id, None, build_payload("Action Two", "action_two"))

    with pytest.raises(ValueError, match="Automation action limit reached"):
        service.create_action(tenant.id, None, build_payload("Action Three", "action_three"))


@pytest.mark.asyncio
async def test_automation_execution_respects_monthly_plan_limit(db: Session):
    tenant = _create_tenant_base(db, name_prefix="automation_execution_limit")
    capabilities = (
        db.query(TenantCapabilities).filter(TenantCapabilities.tenant_id == tenant.id).one()
    )
    capabilities.max_automation_executions_per_month = 1
    db.commit()

    action = _add_action(
        db,
        tenant_id=tenant.id,
        name="Track Order Status",
        slug="track_order_status_limit",
    )
    db.add(
        ActionExecution(
            id=uuid4(),
            tenant_id=tenant.id,
            action_id=action.id,
            trigger_source=ActionTriggerSource.AI_CHAT,
            status=ActionExecutionStatus.SUCCEEDED,
            input_payload={"order_reference": "ORD1234"},
            resolved_request={"method": "GET"},
            normalized_output={"success": True},
            attempt=1,
            meta={},
        )
    )
    db.commit()

    service = AutomationService(db)
    execution = await service.execute_action(
        tenant_id=tenant.id,
        action=action,
        input_payload={"order_reference": "ORD5678"},
        trigger_source=ActionTriggerSource.AI_CHAT,
    )

    assert execution.status == ActionExecutionStatus.BLOCKED_POLICY
    assert execution.error_code == AutomationService.EXECUTION_LIMIT_ERROR_CODE
    assert "monthly automation execution limit reached" in (execution.error_message or "").lower()


def test_action_call_success_interpolates_end_message(client: TestClient, db: Session, mocker):
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
    capabilities = (
        db.query(TenantCapabilities).filter(TenantCapabilities.tenant_id == tenant.id).one()
    )
    capabilities.can_use_lookup_chains = True
    db.commit()

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


@pytest.mark.asyncio
async def test_ai_chat_automation_respects_plan_capability_gate(db: Session):
    tenant = _create_tenant_base(db, name_prefix="ai_chat_gate")
    capabilities = (
        db.query(TenantCapabilities).filter(TenantCapabilities.tenant_id == tenant.id).one()
    )
    capabilities.can_use_ai_chat_automation = False
    db.commit()

    _add_action(
        db,
        tenant_id=tenant.id,
        name="Track Order Status",
        slug="track_order_status_chat",
        available_in_ai_chat=True,
        query_template={"order_reference": "{{order_reference}}"},
        description="Track order status from chat",
    )

    session = ConversationSession(
        id=uuid4(),
        tenant_id=tenant.id,
        user_identifier="ai-chat-gate-user",
        status=SessionStatus.ACTIVE,
        meta={},
    )
    db.add(session)
    db.commit()
    db.refresh(session)

    orchestrator = ChatAutomationOrchestrator(db)
    result = await orchestrator.handle_turn(
        tenant_id=tenant.id,
        session_id=session.id,
        user_message="What is my order status?",
        session_meta=session.meta or {},
        conversation_history=[],
        tenant_ai_config=None,
        company_name=tenant.display_name,
    )

    assert result.handled is False


@pytest.mark.asyncio
async def test_lookup_chain_requires_lookup_chain_capability(db: Session):
    tenant = _create_tenant_base(db, name_prefix="lookup_chain_gate")
    capabilities = (
        db.query(TenantCapabilities).filter(TenantCapabilities.tenant_id == tenant.id).one()
    )
    capabilities.can_use_lookup_chains = False
    db.commit()

    session = ConversationSession(
        id=uuid4(),
        tenant_id=tenant.id,
        user_identifier="lookup-chain-gate-user",
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
    )
    _add_action(
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
    )

    orchestrator = ChatAutomationOrchestrator(db)
    session_meta: dict = {}

    result = await orchestrator.handle_turn(
        tenant_id=tenant.id,
        session_id=session.id,
        user_message="Can you check my account status?",
        session_meta=session_meta,
        conversation_history=[],
        tenant_ai_config=None,
        company_name=tenant.display_name,
    )

    assert result.handled is True
    assert "account" in (result.assistant_text or "").lower()
    assert "email" not in (result.assistant_text or "").lower()


def test_webhook_node_limit_is_enforced_on_flow_create(db: Session):
    _ensure_subscription_plan(
        db,
        tier="professional",
        max_flows=10,
        can_use_webhooks=True,
        max_webhooks_per_flow=3,
    )
    tenant = _create_tenant_base(db, name_prefix="webhook_limit_create")
    tenant.subscription_tier = "professional"
    capabilities = (
        db.query(TenantCapabilities).filter(TenantCapabilities.tenant_id == tenant.id).one()
    )
    capabilities.can_use_webhooks = True
    capabilities.max_webhooks_per_flow = 3
    db.commit()

    flow_service = FlowService(db)

    with pytest.raises(ValueError, match="Webhook node limit reached"):
        flow_service.create_flow(
            FlowCreate(
                tenant_id=tenant.id,
                name="Webhook Limit Flow",
                description="Should exceed webhook count limit",
                trigger_type=FlowTriggerType.MANUAL,
                trigger_config={},
                status=FlowStatus.DRAFT,
                is_active=True,
                version=1,
                nodes={
                    "start_node_id": "webhook_one",
                    "nodes": [
                        {
                            "id": "webhook_one",
                            "type": "webhook",
                            "config": {
                                "url": "https://example.test/one",
                                "method": "POST",
                                "next_node_id": "webhook_two",
                            },
                        },
                        {
                            "id": "webhook_two",
                            "type": "webhook",
                            "config": {
                                "url": "https://example.test/two",
                                "method": "POST",
                                "next_node_id": "webhook_three",
                            },
                        },
                        {
                            "id": "webhook_three",
                            "type": "webhook",
                            "config": {
                                "url": "https://example.test/three",
                                "method": "POST",
                                "next_node_id": "webhook_four",
                            },
                        },
                        {
                            "id": "webhook_four",
                            "type": "webhook",
                            "config": {
                                "url": "https://example.test/four",
                                "method": "POST",
                                "next_node_id": "flow_end",
                            },
                        },
                        {
                            "id": "flow_end",
                            "type": "end",
                            "config": {"message": "Done"},
                        },
                    ],
                    "edges": [],
                },
            )
        )


def test_webhook_node_limit_is_enforced_on_flow_activate(db: Session):
    _ensure_subscription_plan(
        db,
        tier="professional",
        max_flows=10,
        can_use_webhooks=True,
        max_webhooks_per_flow=3,
    )
    tenant = _create_tenant_base(db, name_prefix="webhook_limit_activate")
    tenant.subscription_tier = "professional"
    capabilities = (
        db.query(TenantCapabilities).filter(TenantCapabilities.tenant_id == tenant.id).one()
    )
    capabilities.can_use_webhooks = True
    capabilities.max_webhooks_per_flow = 3
    db.commit()

    flow = _create_manual_flow(
        db,
        tenant_id=tenant.id,
        name="Webhook Activation Limit Flow",
        nodes={
            "start_node_id": "webhook_one",
            "nodes": [
                {
                    "id": "webhook_one",
                    "type": "webhook",
                    "config": {
                        "url": "https://example.test/one",
                        "method": "POST",
                        "next_node_id": "webhook_two",
                    },
                },
                {
                    "id": "webhook_two",
                    "type": "webhook",
                    "config": {
                        "url": "https://example.test/two",
                        "method": "POST",
                        "next_node_id": "webhook_three",
                    },
                },
                {
                    "id": "webhook_three",
                    "type": "webhook",
                    "config": {
                        "url": "https://example.test/three",
                        "method": "POST",
                        "next_node_id": "webhook_four",
                    },
                },
                {
                    "id": "webhook_four",
                    "type": "webhook",
                    "config": {
                        "url": "https://example.test/four",
                        "method": "POST",
                        "next_node_id": "flow_end",
                    },
                },
                {
                    "id": "flow_end",
                    "type": "end",
                    "config": {"message": "Done"},
                },
            ],
            "edges": [],
        },
    )
    db_flow = db.query(Flow).filter(Flow.id == flow.id).one()
    db_flow.status = FlowStatus.DRAFT
    db_flow.is_active = False
    db.commit()

    flow_service = FlowService(db)
    with pytest.raises(ValueError, match="Webhook node limit reached"):
        flow_service.activate_flow(flow.id)


def test_delete_flow_removes_row_instead_of_archiving(db: Session):
    tenant = _create_tenant_base(db, name_prefix="hard_delete_flow")
    flow = _create_manual_flow(
        db,
        tenant_id=tenant.id,
        name="Delete Me",
        nodes={
            "start_node_id": "end",
            "nodes": [{"id": "end", "type": "end", "config": {"message": "Done"}}],
            "edges": [],
        },
    )

    deleted = FlowService(db).delete_flow(flow.id)

    assert deleted is True
    assert db.query(Flow).filter(Flow.id == flow.id).one_or_none() is None
