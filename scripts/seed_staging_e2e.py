"""Create deterministic staging fixtures for live API and UI end-to-end tests.

This script is idempotent and safe for staging/test environments only.
It creates three tenant admins with known capabilities:
- professional tenant for general happy-path and downgrade checks
- lite tenant for lookup/webhook/action-limit gating checks
- quota tenant with execution quota exhausted
"""

# ruff: noqa: E402

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from loguru import logger

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from app.core.encryption import hash_for_lookup
from app.core.security import get_password_hash
from app.db.models.automation import (
    ActionSecret,
    ActionAuthType,
    TenantAction,
    TenantActionStatus,
    TenantActionType,
)
from app.db.models.flow import Flow, FlowStatus, FlowTriggerType
from app.db.models.platform_admin import PlatformAdmin, PlatformAdminRole
from app.db.models.tenant import Tenant, TenantAIConfig, TenantCapabilities, TenantStatus
from app.db.models.user import User, UserRole
from app.db.session import SessionLocal
from app.services.plan_catalog import plan_capabilities

COMMON_PASSWORD = "StagingE2E123$"
BEARER_TOKEN = "staging-sandbox-bearer-token"

# Platform-admin fixtures consumed by the dashboard Playwright suite.
# Defaults match `tests/e2e/helpers.ts` so the workflow needs no new secrets;
# override via env if the GH secrets PLAYWRIGHT_PLATFORM_ADMIN_* are customised.
PLATFORM_E2E_SUPER_EMAIL = "platform-e2e@luranai.com"
PLATFORM_E2E_SUPER_PASSWORD = "platform-e2e-password"
PLATFORM_E2E_ADMIN_EMAIL = "platform-e2e-admin@luranai.com"
PLATFORM_E2E_ADMIN_PASSWORD = "platform-e2e-password"


def _ensure_tenant(
    db,
    *,
    name: str,
    display_name: str,
    public_slug: str,
    subscription_tier: str,
    billing_cycle: str = "monthly",
) -> Tenant:
    tenant = db.query(Tenant).filter(Tenant.name == name).one_or_none()
    if tenant:
        tenant.display_name = display_name
        tenant.public_slug = public_slug
        tenant.status = TenantStatus.ACTIVE
        tenant.subscription_tier = subscription_tier
        tenant.subscription_status = "active"
        tenant.billing_cycle = billing_cycle
        tenant.subscription_starts_at = datetime.now(UTC) - timedelta(days=7)
        tenant.subscription_ends_at = None
        tenant.api_key_hash = get_password_hash(f"{name}-api-key")
        return tenant

    tenant = Tenant(
        id=uuid4(),
        name=name,
        display_name=display_name,
        public_slug=public_slug,
        status=TenantStatus.ACTIVE,
        api_key_hash=get_password_hash(f"{name}-api-key"),
        subscription_tier=subscription_tier,
        subscription_status="active",
        billing_cycle=billing_cycle,
        subscription_starts_at=datetime.now(UTC) - timedelta(days=7),
    )
    db.add(tenant)
    db.flush()
    return tenant


def _ensure_ai_config(db, tenant: Tenant) -> None:
    config = db.query(TenantAIConfig).filter(TenantAIConfig.tenant_id == tenant.id).one_or_none()
    if config:
        config.max_response_tokens = 1200
        config.grounding_requirement = True
        config.brand_voice = config.brand_voice or {}
        config.escalation_keywords = config.escalation_keywords or []
        return

    db.add(
        TenantAIConfig(
            id=uuid4(),
            tenant_id=tenant.id,
            brand_voice={},
            escalation_keywords=[],
            max_response_tokens=1200,
            grounding_requirement=True,
        )
    )


def _ensure_capabilities(
    db,
    tenant: Tenant,
    *,
    can_access_analytics: bool,
    max_knowledge_mb: int,
    max_conversations_per_month: int | None,
    max_knowledge_sources: int | None,
    can_use_url_sources: bool,
    max_team_members: int | None,
    max_automation_actions: int | None,
    max_automation_executions_per_month: int | None,
    can_use_ai_chat_automation: bool,
    can_use_lookup_chains: bool,
    can_use_webhooks: bool,
    max_flows: int | None,
    max_webhooks_per_flow: int | None,
) -> None:
    capabilities = (
        db.query(TenantCapabilities).filter(TenantCapabilities.tenant_id == tenant.id).one_or_none()
    )
    if not capabilities:
        capabilities = TenantCapabilities(id=uuid4(), tenant_id=tenant.id)
        db.add(capabilities)

    capabilities.can_create_tickets = True
    capabilities.can_upload_knowledge = True
    capabilities.can_access_analytics = can_access_analytics
    capabilities.max_knowledge_mb = max_knowledge_mb
    capabilities.max_conversations_per_month = max_conversations_per_month
    capabilities.max_knowledge_sources = max_knowledge_sources
    capabilities.can_use_url_sources = can_use_url_sources
    capabilities.max_team_members = max_team_members
    capabilities.max_automation_actions = max_automation_actions
    capabilities.max_automation_executions_per_month = max_automation_executions_per_month
    capabilities.can_use_ai_chat_automation = can_use_ai_chat_automation
    capabilities.can_use_lookup_chains = can_use_lookup_chains
    capabilities.can_use_webhooks = can_use_webhooks
    capabilities.max_flows = max_flows
    capabilities.max_webhooks_per_flow = max_webhooks_per_flow

    # Entitlement gates come from the tier's plan (single source: plan_catalog), never hand-set.
    plan_values = plan_capabilities(str(tenant.subscription_tier))
    for field in (
        "can_use_sales_close",
        "can_use_crm_notifications",
        "can_use_screen_recording",
        "can_export_ratings",
        "can_use_whatsapp",
        "max_whatsapp_numbers",
    ):
        setattr(capabilities, field, plan_values[field])


def _ensure_user(db, tenant: Tenant, *, email: str, full_name: str) -> User:
    user = db.query(User).filter(User.email_hash == hash_for_lookup(email)).one_or_none()
    if user:
        user.full_name = full_name
        user.tenant_id = tenant.id
        user.role = UserRole.TENANT_ADMIN
        user.is_active = True
        user.hashed_password = get_password_hash(COMMON_PASSWORD)
        return user

    user = User(
        id=uuid4(),
        email=email,
        hashed_password=get_password_hash(COMMON_PASSWORD),
        full_name=full_name,
        role=UserRole.TENANT_ADMIN,
        tenant_id=tenant.id,
        is_active=True,
    )
    db.add(user)
    db.flush()
    return user


def _ensure_platform_admin(
    db,
    *,
    email: str,
    password: str,
    full_name: str,
    role: PlatformAdminRole,
) -> PlatformAdmin:
    admin = (
        db.query(PlatformAdmin)
        .filter(PlatformAdmin.email_hash == hash_for_lookup(email))
        .one_or_none()
    )
    if admin:
        admin.full_name = full_name
        admin.role = role
        admin.is_active = True
        admin.hashed_password = get_password_hash(password)
        return admin

    admin = PlatformAdmin(
        id=uuid4(),
        email=email,
        hashed_password=get_password_hash(password),
        full_name=full_name,
        role=role,
        is_active=True,
    )
    db.add(admin)
    db.flush()
    return admin


def _ensure_secret(db, tenant: Tenant) -> None:
    secret = (
        db.query(ActionSecret)
        .filter(
            ActionSecret.tenant_id == tenant.id, ActionSecret.key_name == "sandbox_bearer_token"
        )
        .one_or_none()
    )
    if not secret:
        secret = ActionSecret(
            id=uuid4(),
            tenant_id=tenant.id,
            provider="sandbox",
            key_name="sandbox_bearer_token",
            secret_value=BEARER_TOKEN,
            status="active",
        )
        db.add(secret)
        return

    secret.provider = "sandbox"
    secret.secret_value = BEARER_TOKEN
    secret.status = "active"


def _action_payloads(base_url: str) -> list[dict]:
    return [
        {
            "name": "Track Order Status",
            "slug": "track_order_status_chat",
            "description": "Track an order by order number.",
            "method": "GET",
            "url_template": f"{base_url}/orders/status",
            "query_template": {"order_reference": "{{order_reference}}"},
            "body_template": {},
            "headers_template": {},
            "input_schema": {"required": ["order_reference"]},
            "allow_in_flows": True,
            "available_in_ai_chat": True,
            "requires_approval": False,
            "is_sensitive": False,
        },
        {
            "name": "Find Account by Email",
            "slug": "find_account_by_email_chat",
            "description": "Lookup an account ID from an email address.",
            "method": "GET",
            "url_template": f"{base_url}/accounts/lookup",
            "query_template": {"email": "{{customer_email}}"},
            "body_template": {},
            "headers_template": {},
            "input_schema": {"required": ["customer_email"]},
            "allow_in_flows": False,
            "available_in_ai_chat": False,
            "requires_approval": False,
            "is_sensitive": False,
        },
        {
            "name": "Check Account Status",
            "slug": "check_account_status_chat",
            "description": "Check a customer account status after resolving the account ID.",
            "method": "GET",
            "url_template": f"{base_url}/accounts/status",
            "query_template": {"account_id": "{{account_id}}"},
            "body_template": {},
            "headers_template": {},
            "input_schema": {
                "required": ["account_id"],
                "lookup_dependencies": {
                    "account_id": {
                        "action_slug": "find_account_by_email_chat",
                        "required_fields": ["customer_email"],
                    }
                },
            },
            "allow_in_flows": True,
            "available_in_ai_chat": True,
            "requires_approval": False,
            "is_sensitive": False,
        },
        {
            "name": "Unlock Suspended Account",
            "slug": "unlock_suspended_account_chat",
            "description": "Unlock a customer account after approval.",
            "method": "POST",
            "url_template": f"{base_url}/accounts/unlock",
            "query_template": {},
            "body_template": {"account_id": "{{account_id}}", "reason": "{{reason}}"},
            "headers_template": {},
            "input_schema": {"required": ["account_id", "reason"]},
            "allow_in_flows": True,
            "available_in_ai_chat": True,
            "requires_approval": True,
            "is_sensitive": True,
        },
        {
            "name": "Create Support Ticket",
            "slug": "create_support_ticket_chat",
            "description": "Create a support ticket from chat.",
            "method": "POST",
            "url_template": f"{base_url}/support/tickets",
            "query_template": {},
            "body_template": {
                "customer_email": "{{customer_email}}",
                "subject": "{{ticket_subject}}",
                "message": "{{ticket_message}}",
            },
            "headers_template": {},
            "input_schema": {"required": ["customer_email", "ticket_subject", "ticket_message"]},
            "allow_in_flows": True,
            "available_in_ai_chat": True,
            "requires_approval": False,
            "is_sensitive": False,
        },
        {
            "name": "Check Appointment Slots",
            "slug": "check_appointment_slots_chat",
            "description": "Check available appointment slots.",
            "method": "GET",
            "url_template": f"{base_url}/appointments/slots",
            "query_template": {"date": "{{appointment_date}}", "service": "{{service_name}}"},
            "body_template": {},
            "headers_template": {"X-API-Key": "inline-demo-key"},
            "input_schema": {"required": ["appointment_date", "service_name"]},
            "allow_in_flows": True,
            "available_in_ai_chat": False,
            "requires_approval": False,
            "is_sensitive": False,
            "auth_type": ActionAuthType.API_KEY_HEADER,
            "auth_config": {"header_name": "X-API-Key", "api_key": "inline-demo-key"},
        },
    ]


def _ensure_action(db, tenant: Tenant, user: User, payload: dict) -> TenantAction:
    action = (
        db.query(TenantAction)
        .filter(TenantAction.tenant_id == tenant.id, TenantAction.slug == payload["slug"])
        .one_or_none()
    )
    if not action:
        action = TenantAction(id=uuid4(), tenant_id=tenant.id, slug=payload["slug"])
        db.add(action)

    action.name = payload["name"]
    action.description = payload["description"]
    action.status = TenantActionStatus.ACTIVE
    action.type = TenantActionType.HTTP
    action.method = payload["method"]
    action.url_template = payload["url_template"]
    action.timeout_seconds = 20
    action.auth_type = payload.get("auth_type", ActionAuthType.BEARER)
    action.auth_config = payload.get(
        "auth_config",
        {"provider": "sandbox", "key_name": "sandbox_bearer_token", "token": BEARER_TOKEN},
    )
    action.headers_template = payload["headers_template"]
    action.query_template = payload["query_template"]
    action.body_template = payload["body_template"]
    action.input_schema = payload["input_schema"]
    action.output_schema = {}
    action.error_mapping = {}
    action.retry_policy = {"max_attempts": 1, "backoff_ms": 0}
    action.is_sensitive = payload["is_sensitive"]
    action.requires_approval = payload["requires_approval"]
    action.allow_in_flows = payload["allow_in_flows"]
    action.available_in_ai_chat = payload["available_in_ai_chat"]
    action.created_by = user.id
    action.updated_by = user.id
    return action


def _ensure_actions(db, tenant: Tenant, user: User, *, count: int) -> list[TenantAction]:
    actions = [
        _ensure_action(
            db,
            tenant,
            user,
            payload,
        )
        for payload in _action_payloads(
            "http://host.docker.internal:8000/api/v1/automation-sandbox"
        )
    ]
    filler_index = 1
    while len(actions) < count:
        filler_slug = f"staging_filler_action_{filler_index}"
        payload = {
            "name": f"Staging Filler Action {filler_index}",
            "slug": filler_slug,
            "description": "Filler action used to keep staging limits deterministic.",
            "method": "GET",
            "url_template": "http://host.docker.internal:8000/api/v1/automation-sandbox/orders/status",
            "query_template": {"order_reference": "{{order_reference}}"},
            "body_template": {},
            "headers_template": {},
            "input_schema": {"required": ["order_reference"]},
            "allow_in_flows": False,
            "available_in_ai_chat": False,
            "requires_approval": False,
            "is_sensitive": False,
        }
        actions.append(_ensure_action(db, tenant, user, payload))
        filler_index += 1
    db.flush()
    return actions[:count]


def _build_action_call_nodes(action: TenantAction) -> dict:
    return {
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
                        "final_error_detail": "$.response.detail",
                    },
                    "on_success_next_node_id": "done",
                    "on_failure_next_node_id": "failure",
                },
            },
            {
                "id": "done",
                "type": "end",
                "config": {
                    "message": "Flow saved status {{final_order_status}} with ETA {{final_eta_date}}."
                },
            },
            {
                "id": "failure",
                "type": "end",
                "config": {
                    "message": "We couldn't complete the order lookup for {{customer_order_number}}. {{final_error_detail}}. Please check the order number and try again."
                },
            },
        ],
        "edges": [],
    }


def _ensure_flow(
    db,
    tenant: Tenant,
    user: User,
    *,
    name: str,
    nodes: dict,
    active: bool = True,
) -> Flow:
    flow = db.query(Flow).filter(Flow.tenant_id == tenant.id, Flow.name == name).one_or_none()
    if not flow:
        flow = Flow(id=uuid4(), tenant_id=tenant.id, name=name)
        db.add(flow)

    flow.description = "Staging E2E flow fixture"
    flow.status = FlowStatus.ACTIVE if active else FlowStatus.DRAFT
    flow.is_active = active
    flow.trigger_type = FlowTriggerType.MANUAL
    flow.trigger_config = {"menu_label": name, "exclude_from_router": False}
    flow.nodes = nodes
    flow.priority = 100
    flow.version = 1
    flow.created_by = user.id
    return flow


def _ensure_flows(
    db, tenant: Tenant, user: User, *, required_count: int, order_action: TenantAction
) -> None:
    _ensure_flow(
        db,
        tenant,
        user,
        name="Staging E2E Order Flow",
        nodes=_build_action_call_nodes(order_action),
        active=True,
    )

    filler_index = 1
    existing_names = {row.name for row in db.query(Flow).filter(Flow.tenant_id == tenant.id).all()}
    while len(existing_names) < required_count:
        name = f"Staging Filler Flow {filler_index}"
        _ensure_flow(
            db,
            tenant,
            user,
            name=name,
            nodes={
                "start_node_id": "end",
                "nodes": [{"id": "end", "type": "end", "config": {"message": "Done"}}],
                "edges": [],
            },
            active=False,
        )
        existing_names.add(name)
        filler_index += 1


def main() -> None:
    db = SessionLocal()
    try:
        pro_tenant = _ensure_tenant(
            db,
            name="staging_e2e_professional",
            display_name="Staging E2E Professional",
            public_slug="staging-e2e-professional",
            subscription_tier="professional",
        )
        lite_tenant = _ensure_tenant(
            db,
            name="staging_e2e_lite",
            display_name="Staging E2E Lite",
            public_slug="staging-e2e-lite",
            subscription_tier="lite",
        )
        quota_tenant = _ensure_tenant(
            db,
            name="staging_e2e_quota",
            display_name="Staging E2E Quota",
            public_slug="staging-e2e-quota",
            subscription_tier="professional",
        )

        for tenant in (pro_tenant, lite_tenant, quota_tenant):
            _ensure_ai_config(db, tenant)
            _ensure_secret(db, tenant)

        _ensure_capabilities(
            db,
            pro_tenant,
            can_access_analytics=True,
            max_knowledge_mb=200,
            max_conversations_per_month=5000,
            max_knowledge_sources=50,
            can_use_url_sources=True,
            max_team_members=10,
            max_automation_actions=15,
            max_automation_executions_per_month=5000,
            can_use_ai_chat_automation=True,
            can_use_lookup_chains=True,
            can_use_webhooks=True,
            max_flows=10,
            max_webhooks_per_flow=3,
        )
        _ensure_capabilities(
            db,
            lite_tenant,
            can_access_analytics=True,
            max_knowledge_mb=50,
            max_conversations_per_month=1000,
            max_knowledge_sources=15,
            can_use_url_sources=True,
            max_team_members=3,
            max_automation_actions=5,
            max_automation_executions_per_month=500,
            can_use_ai_chat_automation=True,
            can_use_lookup_chains=False,
            can_use_webhooks=False,
            max_flows=3,
            max_webhooks_per_flow=0,
        )
        _ensure_capabilities(
            db,
            quota_tenant,
            can_access_analytics=True,
            max_knowledge_mb=200,
            max_conversations_per_month=5000,
            max_knowledge_sources=50,
            can_use_url_sources=True,
            max_team_members=10,
            max_automation_actions=15,
            max_automation_executions_per_month=0,
            can_use_ai_chat_automation=True,
            can_use_lookup_chains=True,
            can_use_webhooks=True,
            max_flows=10,
            max_webhooks_per_flow=3,
        )

        pro_user = _ensure_user(
            db, pro_tenant, email="staging-e2e-pro@luranai.com", full_name="Staging Pro Admin"
        )
        lite_user = _ensure_user(
            db, lite_tenant, email="staging-e2e-lite@luranai.com", full_name="Staging Lite Admin"
        )
        quota_user = _ensure_user(
            db, quota_tenant, email="staging-e2e-quota@luranai.com", full_name="Staging Quota Admin"
        )

        db.flush()

        pro_actions = _ensure_actions(db, pro_tenant, pro_user, count=6)
        _ensure_flows(db, pro_tenant, pro_user, required_count=4, order_action=pro_actions[0])

        lite_actions = _ensure_actions(db, lite_tenant, lite_user, count=5)
        _ensure_flows(
            db,
            lite_tenant,
            lite_user,
            required_count=1,
            order_action=lite_actions[0],
        )

        quota_actions = _ensure_actions(db, quota_tenant, quota_user, count=3)
        _ensure_flows(
            db,
            quota_tenant,
            quota_user,
            required_count=1,
            order_action=quota_actions[0],
        )

        _ensure_platform_admin(
            db,
            email=PLATFORM_E2E_SUPER_EMAIL,
            password=PLATFORM_E2E_SUPER_PASSWORD,
            full_name="Staging Platform Super",
            role=PlatformAdminRole.SUPER_ADMIN,
        )
        _ensure_platform_admin(
            db,
            email=PLATFORM_E2E_ADMIN_EMAIL,
            password=PLATFORM_E2E_ADMIN_PASSWORD,
            full_name="Staging Platform Admin",
            role=PlatformAdminRole.ADMIN,
        )

        db.commit()
        logger.success("Staging E2E fixtures are ready.")
        logger.info("Tenant admins:")
        logger.info("  - staging-e2e-pro@luranai.com")
        logger.info("  - staging-e2e-lite@luranai.com")
        logger.info("  - staging-e2e-quota@luranai.com")
        logger.info(f"Shared password: {COMMON_PASSWORD}")
        logger.info("Platform admins:")
        logger.info(f"  - {PLATFORM_E2E_SUPER_EMAIL} (SUPER_ADMIN)")
        logger.info(f"  - {PLATFORM_E2E_ADMIN_EMAIL} (ADMIN)")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
