"""Create Luran support flows (part 2) using current Flow JSON schema.

Creates 2 flows:
4. Account & Subscription
5. Troubleshooting & Errors

Usage:
    python scripts/create_luran_support_flows_part2.py --tenant-id <tenant_uuid>
    python scripts/create_luran_support_flows_part2.py --tenant-name innocent_world

Notes:
- This script upserts flows by name (update if exists, create if missing).
- Subscription details are aligned with scripts/seed_data.py and subscription endpoints.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.db.models.flow import Flow, FlowStatus, FlowTriggerType
from app.db.models.tenant import Tenant
from app.db.session import SessionLocal


def _node(node_id: str, node_type: str, config: dict[str, Any]) -> dict[str, Any]:
    return {"id": node_id, "type": node_type, "config": config}


def _resolve_tenant(db: Session, tenant_id: str | None, tenant_name: str | None) -> Tenant:
    tenant_id = tenant_id or os.getenv("LURAN_TENANT_ID")
    tenant_name = tenant_name or os.getenv("LURAN_TENANT_NAME")

    if tenant_id:
        tenant = db.execute(select(Tenant).where(Tenant.id == UUID(tenant_id))).scalar_one_or_none()
        if tenant:
            return tenant
        raise ValueError(f"Tenant not found for id={tenant_id}")

    if tenant_name:
        tenant = db.execute(select(Tenant).where(Tenant.name == tenant_name)).scalar_one_or_none()
        if tenant:
            return tenant
        raise ValueError(f"Tenant not found for name={tenant_name}")

    raise ValueError("Provide --tenant-id or --tenant-name")


def _upsert_flow(
    db: Session,
    tenant_id: UUID,
    *,
    name: str,
    description: str,
    priority: int,
    trigger_keywords: list[str],
    menu_label: str,
    menu_description: str,
    nodes: dict[str, Any],
) -> Flow:
    stmt = select(Flow).where(Flow.tenant_id == tenant_id, Flow.name == name)
    existing = db.execute(stmt).scalar_one_or_none()

    trigger_config = {
        "keywords": trigger_keywords,
        "match_mode": "word",
        "menu_label": menu_label,
        "menu_description": menu_description,
        "exclude_from_router": False,
    }

    if existing:
        existing.description = description
        existing.status = FlowStatus.ACTIVE
        existing.is_active = True
        existing.trigger_type = FlowTriggerType.KEYWORD
        existing.trigger_config = trigger_config
        existing.nodes = nodes
        existing.priority = priority
        existing.version = (existing.version or 1) + 1
        db.flush()
        return existing

    flow = Flow(
        tenant_id=tenant_id,
        name=name,
        description=description,
        status=FlowStatus.ACTIVE,
        is_active=True,
        trigger_type=FlowTriggerType.KEYWORD,
        trigger_config=trigger_config,
        nodes=nodes,
        priority=priority,
        version=1,
    )
    db.add(flow)
    db.flush()
    return flow


def build_account_subscription_flow() -> dict[str, Any]:
    return {
        "start_node_id": "billing_menu",
        "nodes": [
            _node(
                "billing_menu",
                "question",
                {
                    "question": "What account or subscription help do you need?",
                    "save_to": "billing_topic",
                    "options": [
                        {"label": "See plans and pricing", "value": "plans", "next_node_id": "plans_message"},
                        {
                            "label": "Trial details",
                            "value": "trial",
                            "next_node_id": "trial_message",
                        },
                        {
                            "label": "Upgrade or downgrade",
                            "value": "change_plan",
                            "next_node_id": "change_plan_message",
                        },
                        {
                            "label": "Cancel subscription",
                            "value": "cancel",
                            "next_node_id": "cancel_message",
                        },
                        {
                            "label": "Payment or invoice issue",
                            "value": "payment_issue",
                            "next_node_id": "payment_issue_input",
                        },
                        {
                            "label": "Talk to support",
                            "value": "human",
                            "next_node_id": "escalate",
                        },
                    ],
                },
            ),
            _node(
                "plans_message",
                "message",
                {
                    "message": (
                        "Current plans:\n"
                        "- Trial: NGN 0 (200 conversations/month, 10MB, 1 team member).\n"
                        "- Lite: NGN 9,000/month or NGN 90,000/year.\n"
                        "- Professional: NGN 40,000/month or NGN 400,000/year.\n"
                        "- Growth: NGN 80,000/month or NGN 800,000/year.\n"
                        "- Enterprise: custom pricing via sales."
                    ),
                    "next_node_id": "plan_follow_up",
                },
            ),
            _node(
                "trial_message",
                "message",
                {
                    "message": (
                        "Trial facts:\n"
                        "- Trial status starts as trialing.\n"
                        "- Trial expiration is handled as a 30-day trial window in tenant lifecycle.\n"
                        "- Trial includes limited conversations, storage, and team seats.\n"
                        "- Upgrade from Subscription settings to continue full access."
                    ),
                    "next_node_id": "plan_follow_up",
                },
            ),
            _node(
                "change_plan_message",
                "message",
                {
                    "message": (
                        "Plan change rules:\n"
                        "- Upgrade payment initialization supports lite/professional/growth.\n"
                        "- Billing cycle must be monthly or annual.\n"
                        "- Downgrade validates your current usage against lower-tier limits.\n"
                        "- Downgrade from trial is not allowed; upgrade first."
                    ),
                    "next_node_id": "plan_follow_up",
                },
            ),
            _node(
                "cancel_message",
                "message",
                {
                    "message": (
                        "Cancellation options:\n"
                        "- Immediate cancellation: access ends now.\n"
                        "- End-of-cycle cancellation: access remains until current period ends.\n"
                        "- Trial subscriptions cannot use paid cancellation flow."
                    ),
                    "next_node_id": "plan_follow_up",
                },
            ),
            _node(
                "payment_issue_input",
                "text_input",
                {
                    "prompt": "Share your payment/invoice issue with any transaction reference you have.",
                    "save_to": "payment_issue_details",
                    "validation": {"required": True, "min_length": 8},
                    "acknowledgment": "Thanks. I captured that.",
                    "next_node_id": "payment_issue_ai",
                },
            ),
            _node(
                "payment_issue_ai",
                "ai_action",
                {
                    "prompt": (
                        "Customer reported this payment issue: {{payment_issue_details}}. "
                        "Give a concise next-step checklist and include when to escalate to billing support."
                    ),
                    "save_to": "payment_issue_guidance",
                    "use_rag": True,
                    "auto_retrieve": True,
                    "max_tokens": 350,
                    "max_history_turns": 8,
                    "next_node_id": "plan_follow_up",
                },
            ),
            _node(
                "plan_follow_up",
                "question",
                {
                    "question": "Would you like to continue in billing support?",
                    "save_to": "billing_follow_up",
                    "options": [
                        {
                            "label": "Yes, ask another billing question",
                            "value": "continue",
                            "next_node_id": "billing_menu",
                        },
                        {
                            "label": "No, this is resolved",
                            "value": "resolved",
                            "next_node_id": "end",
                        },
                        {
                            "label": "Talk to support",
                            "value": "human",
                            "next_node_id": "escalate",
                        },
                    ],
                },
            ),
            _node(
                "escalate",
                "escalate",
                {
                    "reason": "Account or subscription issue needs human support",
                    "message": "I will escalate this to our billing/support team now.",
                },
            ),
            _node("end", "end", {"message": "Thanks. Your account request is complete."}),
        ],
        "edges": [],
    }


def build_troubleshooting_errors_flow() -> dict[str, Any]:
    return {
        "start_node_id": "issue_type",
        "nodes": [
            _node(
                "issue_type",
                "question",
                {
                    "question": "Which issue are you troubleshooting?",
                    "save_to": "tech_issue",
                    "options": [
                        {
                            "label": "AI response quality issue",
                            "value": "ai_quality",
                            "next_node_id": "ai_quality_help",
                        },
                        {
                            "label": "Flow not triggering as expected",
                            "value": "flow_trigger",
                            "next_node_id": "flow_help",
                        },
                        {
                            "label": "Dashboard or API error",
                            "value": "api_error",
                            "next_node_id": "api_error_input",
                        },
                        {
                            "label": "Live chat or agent message issue",
                            "value": "chat_issue",
                            "next_node_id": "chat_help",
                        },
                        {
                            "label": "Talk to support",
                            "value": "human",
                            "next_node_id": "escalate",
                        },
                    ],
                },
            ),
            _node(
                "ai_quality_help",
                "message",
                {
                    "message": (
                        "AI quality checklist:\n"
                        "1. Ensure core FAQs/policies are uploaded and processed.\n"
                        "2. Remove duplicate or conflicting documents.\n"
                        "3. Ask specific user questions in flow prompts.\n"
                        "4. Re-test with 3 to 5 common customer intents."
                    ),
                    "next_node_id": "resolved_check",
                },
            ),
            _node(
                "flow_help",
                "message",
                {
                    "message": (
                        "Flow trigger checklist:\n"
                        "1. Flow must be active and status=active.\n"
                        "2. Trigger keywords should avoid reserved global start routing behavior.\n"
                        "3. Start node must exist and be reachable.\n"
                        "4. Option branches must have valid next_node_id values."
                    ),
                    "next_node_id": "resolved_check",
                },
            ),
            _node(
                "chat_help",
                "message",
                {
                    "message": (
                        "Live chat checklist:\n"
                        "1. Confirm backend API is reachable from dashboard/chat apps.\n"
                        "2. Confirm session is open and not resolved/closed.\n"
                        "3. Check browser network for agent-message endpoint errors.\n"
                        "4. Verify WebSocket reconnect after reload."
                    ),
                    "next_node_id": "resolved_check",
                },
            ),
            _node(
                "api_error_input",
                "text_input",
                {
                    "prompt": "Paste the exact error message and endpoint if available.",
                    "save_to": "api_error_details",
                    "validation": {"required": True, "min_length": 10},
                    "acknowledgment": "Captured. Let me suggest next steps.",
                    "next_node_id": "api_error_ai",
                },
            ),
            _node(
                "api_error_ai",
                "ai_action",
                {
                    "prompt": (
                        "Troubleshoot this Luran technical issue: {{api_error_details}}. "
                        "Provide short root-cause hypotheses and safe verification steps."
                    ),
                    "save_to": "api_error_guidance",
                    "use_rag": True,
                    "auto_retrieve": True,
                    "max_tokens": 350,
                    "max_history_turns": 8,
                    "next_node_id": "resolved_check",
                },
            ),
            _node(
                "resolved_check",
                "question",
                {
                    "question": "Did this resolve the issue?",
                    "save_to": "tech_resolved",
                    "options": [
                        {"label": "Yes", "value": "yes", "next_node_id": "end"},
                        {
                            "label": "No, escalate to support",
                            "value": "no",
                            "next_node_id": "escalate",
                        },
                    ],
                },
            ),
            _node(
                "escalate",
                "escalate",
                {
                    "reason": "Technical troubleshooting issue unresolved",
                    "message": "I will escalate this to Luran technical support.",
                },
            ),
            _node("end", "end", {"message": "Great. Let us know if you need more help."}),
        ],
        "edges": [],
    }


def create_flows(db: Session, tenant: Tenant) -> list[Flow]:
    created_or_updated: list[Flow] = []

    flows = [
        {
            "name": "Account & Subscription",
            "description": "Handle plan, pricing, billing, cancellation, and payment support questions.",
            "priority": 92,
            "trigger_keywords": ["billing", "pricing", "subscription", "upgrade", "downgrade"],
            "menu_label": "Account & Billing",
            "menu_description": "Plans, pricing, trial, payment, and cancellation support.",
            "nodes": build_account_subscription_flow(),
        },
        {
            "name": "Troubleshooting & Errors",
            "description": "Diagnose AI, flow, dashboard/API, and live-chat issues with escalation fallback.",
            "priority": 88,
            "trigger_keywords": ["error", "issue", "troubleshoot", "bug", "flow problem"],
            "menu_label": "Troubleshooting",
            "menu_description": "Fix technical and runtime support issues.",
            "nodes": build_troubleshooting_errors_flow(),
        },
    ]

    for cfg in flows:
        flow = _upsert_flow(
            db,
            tenant.id,
            name=cfg["name"],
            description=cfg["description"],
            priority=cfg["priority"],
            trigger_keywords=cfg["trigger_keywords"],
            menu_label=cfg["menu_label"],
            menu_description=cfg["menu_description"],
            nodes=cfg["nodes"],
        )
        created_or_updated.append(flow)

    return created_or_updated


def main() -> None:
    parser = argparse.ArgumentParser(description="Create Luran support flows (part 2)")
    parser.add_argument("--tenant-id", type=str, help="Tenant UUID")
    parser.add_argument("--tenant-name", type=str, help="Tenant name (e.g. innocent_world)")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        tenant = _resolve_tenant(db, args.tenant_id, args.tenant_name)
        flows = create_flows(db, tenant)
        db.commit()

        print(f"Tenant: {tenant.display_name} ({tenant.id})")
        for flow in flows:
            print(f"- Upserted: {flow.name} (id={flow.id}, version={flow.version})")

    except Exception as exc:
        db.rollback()
        raise SystemExit(f"Failed to create flows: {exc}") from exc
    finally:
        db.close()


if __name__ == "__main__":
    main()
