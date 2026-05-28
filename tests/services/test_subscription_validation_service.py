from __future__ import annotations

from uuid import uuid4

from sqlalchemy.orm import Session

from app.db.models.automation import (
    ActionAuthType,
    TenantAction,
    TenantActionStatus,
    TenantActionType,
)
from app.db.models.flow import Flow, FlowStatus, FlowTriggerType
from app.db.models.subscription_plan import SubscriptionPlan
from app.db.models.tenant import Tenant, TenantAIConfig, TenantCapabilities
from app.services.subscription_validation_service import SubscriptionValidationService


def _create_tenant(db: Session, *, name_prefix: str) -> Tenant:
    tenant = Tenant(
        id=uuid4(),
        name=f"{name_prefix}_{uuid4().hex[:8]}",
        display_name=f"{name_prefix.title()} Tenant",
        api_key_hash="test_hash",
        subscription_tier="professional",
        subscription_status="active",
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
            max_automation_actions=15,
            max_automation_executions_per_month=5000,
            can_use_ai_chat_automation=True,
            can_use_lookup_chains=True,
            max_flows=10,
        )
    )
    db.commit()
    db.refresh(tenant)
    return tenant


def _ensure_plan(
    db: Session,
    *,
    tier: str,
    max_flows: int | None,
    max_automation_actions: int | None,
) -> None:
    plan = db.query(SubscriptionPlan).filter(SubscriptionPlan.tier == tier).one_or_none()
    if not plan:
        plan = SubscriptionPlan(
            tier=tier,
            display_name=tier.title(),
            description=f"{tier.title()} plan",
            monthly_price_kobo=100000,
            annual_price_kobo=1000000,
            can_access_analytics=True,
            max_knowledge_sources=20,
            can_use_url_sources=True,
            max_knowledge_mb=200,
            max_conversations_per_month=5000,
            max_team_members=10,
            max_response_tokens=2000,
            max_automation_actions=max_automation_actions,
            max_automation_executions_per_month=5000,
            can_use_ai_chat_automation=True,
            can_use_lookup_chains=True,
            max_flows=max_flows,
            max_nodes_per_flow=20,
            can_use_webhooks=True,
            max_webhooks_per_flow=3,
            features=[],
            is_active=True,
            sort_order=1,
        )
        db.add(plan)
    else:
        plan.max_flows = max_flows
        plan.max_automation_actions = max_automation_actions
        plan.is_active = True
    db.commit()


def test_validate_tier_change_blocks_when_flow_count_exceeds_new_tier(db: Session):
    _ensure_plan(db, tier="lite", max_flows=3, max_automation_actions=5)
    tenant = _create_tenant(db, name_prefix="downgrade_flows")

    for index in range(4):
        db.add(
            Flow(
                id=uuid4(),
                tenant_id=tenant.id,
                name=f"Flow {index}",
                description="Test flow",
                status=FlowStatus.ACTIVE,
                is_active=True,
                trigger_type=FlowTriggerType.MANUAL,
                trigger_config={},
                nodes={
                    "start_node_id": "end",
                    "nodes": [{"id": "end", "type": "end", "config": {"message": "Done"}}],
                    "edges": [],
                },
                priority=100,
                version=1,
            )
        )
    db.commit()

    result = SubscriptionValidationService(db).validate_tier_change(tenant.id, "lite")

    assert result.valid is False
    assert any(v.field == "flows" and v.allowed_value == 3 for v in result.violations)


def test_validate_tier_change_blocks_when_action_count_exceeds_new_tier(db: Session):
    _ensure_plan(db, tier="lite", max_flows=3, max_automation_actions=5)
    tenant = _create_tenant(db, name_prefix="downgrade_actions")

    for index in range(6):
        db.add(
            TenantAction(
                id=uuid4(),
                tenant_id=tenant.id,
                name=f"Action {index}",
                slug=f"action-{index}",
                status=TenantActionStatus.ACTIVE,
                type=TenantActionType.HTTP,
                method="GET",
                url_template="https://example.test/api",
                auth_type=ActionAuthType.NONE,
                auth_config={},
                headers_template={},
                query_template={},
                body_template={},
                input_schema={},
                output_schema={},
                error_mapping={},
                retry_policy={"max_attempts": 1, "backoff_ms": 0},
                allow_in_flows=False,
                available_in_ai_chat=False,
            )
        )
    db.commit()

    result = SubscriptionValidationService(db).validate_tier_change(tenant.id, "lite")

    assert result.valid is False
    assert any(v.field == "automation_actions" and v.allowed_value == 5 for v in result.violations)
