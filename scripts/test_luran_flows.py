"""Validate Luran support flows using current Flow JSON schema.

Checks:
1. Required flow names exist
2. Flow is active and keyword-triggered
3. start_node_id exists in node set
4. All next_node references are valid
5. Flow has at least one end or escalate node

Usage:
    python scripts/test_luran_flows.py --tenant-id <tenant_uuid>
    python scripts/test_luran_flows.py --tenant-name innocent_world
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from uuid import UUID

from sqlalchemy import select

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.db.models.flow import Flow, FlowTriggerType
from app.db.models.tenant import Tenant
from app.db.session import SessionLocal

REQUIRED_FLOWS = [
    "Getting Started",
    "Knowledge Upload Issues",
    "Widget Integration Issues",
    "Account & Subscription",
    "Troubleshooting & Errors",
]


def resolve_tenant_id(tenant_id: str | None, tenant_name: str | None) -> UUID:
    tenant_id = tenant_id or os.getenv("LURAN_TENANT_ID")
    tenant_name = tenant_name or os.getenv("LURAN_TENANT_NAME")

    db = SessionLocal()
    try:
        if tenant_id:
            tenant = db.execute(select(Tenant).where(Tenant.id == UUID(tenant_id))).scalar_one_or_none()
            if not tenant:
                raise ValueError(f"Tenant not found for id={tenant_id}")
            return tenant.id

        if tenant_name:
            tenant = db.execute(select(Tenant).where(Tenant.name == tenant_name)).scalar_one_or_none()
            if not tenant:
                raise ValueError(f"Tenant not found for name={tenant_name}")
            return tenant.id

        raise ValueError("Provide --tenant-id or --tenant-name")
    finally:
        db.close()


def validate_flow_structure(flow: Flow) -> list[str]:
    errors: list[str] = []
    nodes_cfg = flow.nodes or {}
    nodes = nodes_cfg.get("nodes", [])
    start_node_id = nodes_cfg.get("start_node_id")

    if not isinstance(nodes, list) or not nodes:
        return ["nodes list missing or empty"]

    node_map = {
        n.get("id"): n
        for n in nodes
        if isinstance(n, dict) and isinstance(n.get("id"), str) and n.get("id")
    }

    if not start_node_id or start_node_id not in node_map:
        errors.append("start_node_id missing or invalid")

    has_terminal = False
    for node_id, node in node_map.items():
        node_type = node.get("type")
        config = node.get("config") or {}

        if node_type in {"end", "escalate"}:
            has_terminal = True

        next_node_id = config.get("next_node_id")
        if isinstance(next_node_id, str) and next_node_id and next_node_id not in node_map:
            errors.append(f"{node_id} -> invalid next_node_id '{next_node_id}'")

        default_node_id = config.get("default_node_id")
        if isinstance(default_node_id, str) and default_node_id and default_node_id not in node_map:
            errors.append(f"{node_id} -> invalid default_node_id '{default_node_id}'")

        options = config.get("options", [])
        if isinstance(options, list):
            for opt in options:
                if not isinstance(opt, dict):
                    continue
                opt_next = opt.get("next_node_id")
                if isinstance(opt_next, str) and opt_next and opt_next not in node_map:
                    errors.append(f"{node_id} -> option invalid next_node_id '{opt_next}'")

        conditions = config.get("conditions", [])
        if isinstance(conditions, list):
            for cond in conditions:
                if not isinstance(cond, dict):
                    continue
                cond_next = cond.get("next_node_id")
                if isinstance(cond_next, str) and cond_next and cond_next not in node_map:
                    errors.append(f"{node_id} -> condition invalid next_node_id '{cond_next}'")

    if not has_terminal:
        errors.append("missing terminal node (end/escalate)")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate Luran support flows")
    parser.add_argument("--tenant-id", type=str, help="Tenant UUID")
    parser.add_argument("--tenant-name", type=str, help="Tenant name")
    args = parser.parse_args()

    tenant_id = resolve_tenant_id(args.tenant_id, args.tenant_name)

    db = SessionLocal()
    try:
        flows = db.execute(select(Flow).where(Flow.tenant_id == tenant_id)).scalars().all()
        by_name = {flow.name: flow for flow in flows}

        errors: list[str] = []

        for name in REQUIRED_FLOWS:
            flow = by_name.get(name)
            if not flow:
                errors.append(f"Missing required flow: {name}")
                continue

            if not flow.is_active:
                errors.append(f"{name}: flow is not active")

            if flow.trigger_type != FlowTriggerType.KEYWORD:
                errors.append(f"{name}: trigger_type should be keyword")

            keywords = (flow.trigger_config or {}).get("keywords", [])
            if not isinstance(keywords, list) or not keywords:
                errors.append(f"{name}: trigger keywords missing")

            structure_errors = validate_flow_structure(flow)
            errors.extend([f"{name}: {err}" for err in structure_errors])

        if errors:
            print("Validation failed:")
            for err in errors:
                print(f"- {err}")
            return 1

        print("All required Luran support flows are valid.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
