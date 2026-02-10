"""Create Luran support flows (part 1) using current Flow JSON schema.

Creates 3 flows:
1. Getting Started
2. Knowledge Upload Issues
3. Widget Integration Issues

Usage:
    python scripts/create_luran_support_flows.py --tenant-id <tenant_uuid>
    python scripts/create_luran_support_flows.py --tenant-name innocent_world

Notes:
- This script upserts flows by name (update if exists, create if missing).
- Uses modern flow schema: trigger_type/trigger_config/nodes(start_node_id, nodes, edges).
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


def build_getting_started_flow() -> dict[str, Any]:
    return {
        "start_node_id": "welcome",
        "nodes": [
            _node(
                "welcome",
                "question",
                {
                    "question": "Welcome to Luran. What do you want to set up first?",
                    "save_to": "setup_topic",
                    "options": [
                        {
                            "label": "Create account and verify email",
                            "value": "account_setup",
                            "next_node_id": "account_setup_steps",
                        },
                        {
                            "label": "Upload knowledge",
                            "value": "knowledge_upload",
                            "next_node_id": "knowledge_steps",
                        },
                        {
                            "label": "Install chat widget",
                            "value": "widget_install",
                            "next_node_id": "widget_steps",
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
                "account_setup_steps",
                "message",
                {
                    "message": (
                        "Account setup checklist:\n"
                        "1. Register company and admin account.\n"
                        "2. Verify your email link.\n"
                        "3. Sign in to dashboard.\n"
                        "4. Create a PUBLIC API key from Settings -> API Keys."
                    ),
                    "next_node_id": "more_help",
                },
            ),
            _node(
                "knowledge_steps",
                "message",
                {
                    "message": (
                        "Knowledge upload checklist:\n"
                        "1. Go to dashboard Knowledge page.\n"
                        "2. Upload .txt, .md, .pdf, or .docx files.\n"
                        "3. File size limit per upload: 10MB.\n"
                        "4. Wait for job processing to complete."
                    ),
                    "next_node_id": "more_help",
                },
            ),
            _node(
                "widget_steps",
                "message",
                {
                    "message": (
                        "Widget install steps:\n"
                        "1. Get install snippet from Settings -> Widget -> Install Code.\n"
                        "2. Replace YOUR_PUBLIC_API_KEY_HERE with a PUBLIC key (pk_live_...).\n"
                        "3. Paste snippet before </body> on your website.\n"
                        "4. Publish site and test the floating button."
                    ),
                    "next_node_id": "more_help",
                },
            ),
            _node(
                "more_help",
                "question",
                {
                    "question": "Do you want help with another setup step?",
                    "save_to": "restart_setup",
                    "options": [
                        {"label": "Yes", "value": "yes", "next_node_id": "welcome"},
                        {"label": "No", "value": "no", "next_node_id": "end"},
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
                    "reason": "User requested human help during getting started flow",
                    "message": "I will connect you with support so we can help directly.",
                },
            ),
            _node(
                "end",
                "end",
                {"message": "Great. You can return anytime for setup guidance."},
            ),
        ],
        "edges": [],
    }


def build_knowledge_upload_issues_flow() -> dict[str, Any]:
    return {
        "start_node_id": "issue_menu",
        "nodes": [
            _node(
                "issue_menu",
                "question",
                {
                    "question": "What issue are you seeing with knowledge upload?",
                    "save_to": "knowledge_issue",
                    "options": [
                        {
                            "label": "File rejected or unsupported",
                            "value": "unsupported",
                            "next_node_id": "unsupported_help",
                        },
                        {
                            "label": "File too large",
                            "value": "too_large",
                            "next_node_id": "size_help",
                        },
                        {
                            "label": "Processing job failed",
                            "value": "job_failed",
                            "next_node_id": "job_failed_help",
                        },
                        {
                            "label": "Answers are not accurate",
                            "value": "poor_answers",
                            "next_node_id": "quality_help",
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
                "unsupported_help",
                "message",
                {
                    "message": (
                        "Supported file formats are: .txt, .md, .pdf, .docx. "
                        "If your file is another format, export it to one of these and upload again."
                    ),
                    "next_node_id": "resolved_check",
                },
            ),
            _node(
                "size_help",
                "message",
                {
                    "message": (
                        "Current upload limit is 10MB per file. "
                        "Split large files into smaller parts, then upload each part separately."
                    ),
                    "next_node_id": "resolved_check",
                },
            ),
            _node(
                "job_failed_help",
                "message",
                {
                    "message": (
                        "For failed processing jobs:\n"
                        "1. Re-upload the file once.\n"
                        "2. Confirm the file has readable text (not scanned image only).\n"
                        "3. Check your plan storage and source limits in Subscription settings."
                    ),
                    "next_node_id": "resolved_check",
                },
            ),
            _node(
                "quality_help",
                "ai_action",
                {
                    "prompt": (
                        "User reports poor answer quality after upload. Give concise steps to improve "
                        "knowledge quality: cleaner source files, focused docs, updated FAQs, and re-upload guidance."
                    ),
                    "save_to": "quality_guidance",
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
                    "question": "Did this resolve your upload issue?",
                    "save_to": "knowledge_resolved",
                    "options": [
                        {"label": "Yes", "value": "yes", "next_node_id": "end"},
                        {
                            "label": "No, talk to support",
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
                    "reason": "Knowledge upload issue unresolved",
                    "message": "I will escalate this to support with your issue details.",
                },
            ),
            _node("end", "end", {"message": "Great. Reach out anytime if you need more help."}),
        ],
        "edges": [],
    }


def build_widget_integration_issues_flow() -> dict[str, Any]:
    return {
        "start_node_id": "widget_issue_menu",
        "nodes": [
            _node(
                "widget_issue_menu",
                "question",
                {
                    "question": "What do you need help with for widget integration?",
                    "save_to": "widget_issue",
                    "options": [
                        {
                            "label": "Widget not showing on website",
                            "value": "not_showing",
                            "next_node_id": "not_showing_help",
                        },
                        {
                            "label": "API key or authorization error",
                            "value": "api_key_error",
                            "next_node_id": "api_key_help",
                        },
                        {
                            "label": "Need install code steps",
                            "value": "install_steps",
                            "next_node_id": "install_help",
                        },
                        {
                            "label": "Styling or greeting update",
                            "value": "styling",
                            "next_node_id": "styling_help",
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
                "not_showing_help",
                "message",
                {
                    "message": (
                        "Widget visibility checklist:\n"
                        "1. Confirm snippet is placed before </body>.\n"
                        "2. Confirm tenantId is your tenant UUID (not tenant name).\n"
                        "3. Confirm API key is a PUBLIC key.\n"
                        "4. Publish your website changes and hard refresh browser."
                    ),
                    "next_node_id": "resolved_check",
                },
            ),
            _node(
                "api_key_help",
                "message",
                {
                    "message": (
                        "Use a PUBLIC API key from Settings -> API Keys. "
                        "Do not use secret keys in widget code. "
                        "After replacing key, refresh your website and retest."
                    ),
                    "next_node_id": "resolved_check",
                },
            ),
            _node(
                "install_help",
                "message",
                {
                    "message": (
                        "Install snippet format:\n"
                        "<script>window.luranSettings={tenantId:'YOUR_TENANT_UUID',apiKey:'pk_live_...'};</script>\n"
                        "<script src='https://cdn.luranai.com/widget/v1/widget.js' async defer></script>\n"
                        "Paste before </body>, then publish your site."
                    ),
                    "next_node_id": "resolved_check",
                },
            ),
            _node(
                "styling_help",
                "message",
                {
                    "message": (
                        "Widget styling and greeting are managed in Settings -> Widget Configuration. "
                        "You can update colors, logo, greeting message, and position there."
                    ),
                    "next_node_id": "resolved_check",
                },
            ),
            _node(
                "resolved_check",
                "question",
                {
                    "question": "Did this resolve your widget issue?",
                    "save_to": "widget_resolved",
                    "options": [
                        {"label": "Yes", "value": "yes", "next_node_id": "end"},
                        {
                            "label": "No, talk to support",
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
                    "reason": "Widget integration issue unresolved",
                    "message": "I will connect you to support for direct integration help.",
                },
            ),
            _node("end", "end", {"message": "Perfect. Your widget setup should be ready now."}),
        ],
        "edges": [],
    }


def create_flows(db: Session, tenant: Tenant) -> list[Flow]:
    created_or_updated: list[Flow] = []

    flows = [
        {
            "name": "Getting Started",
            "description": "Guide tenant admins through account setup, knowledge upload, and widget install.",
            "priority": 95,
            "trigger_keywords": ["getting started", "setup help", "onboarding", "new account"],
            "menu_label": "Getting Started",
            "menu_description": "Set up your account, knowledge, and widget quickly.",
            "nodes": build_getting_started_flow(),
        },
        {
            "name": "Knowledge Upload Issues",
            "description": "Troubleshoot file upload, processing, and response-quality issues.",
            "priority": 90,
            "trigger_keywords": ["knowledge upload", "upload failed", "document issue", "faq upload"],
            "menu_label": "Knowledge Upload",
            "menu_description": "Fix document upload and answer quality issues.",
            "nodes": build_knowledge_upload_issues_flow(),
        },
        {
            "name": "Widget Integration Issues",
            "description": "Resolve widget installation, visibility, API key, and configuration problems.",
            "priority": 90,
            "trigger_keywords": ["widget", "install widget", "embed chat", "widget not showing"],
            "menu_label": "Widget Integration",
            "menu_description": "Get your website chat widget working correctly.",
            "nodes": build_widget_integration_issues_flow(),
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
    parser = argparse.ArgumentParser(description="Create Luran support flows (part 1)")
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
