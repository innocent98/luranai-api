"""Normalize stored flow/template edge shape to canonical source/target.

This script updates JSON payloads in:
- flows.nodes.edges
- flow_templates.template_nodes.edges

It converts legacy edge keys:
- from -> source
- to -> target

Usage:
    python scripts/normalize_flow_edges.py --dry-run
    python scripts/normalize_flow_edges.py
    python scripts/normalize_flow_edges.py --tenant-name innocent_world
    python scripts/normalize_flow_edges.py --tenant-id <tenant_uuid>
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.db.models.flow import Flow, FlowTemplate
from app.db.models.tenant import Tenant
from app.db.session import SessionLocal


def _normalize_edges_container(container: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Return normalized container and whether it changed."""
    if not isinstance(container, dict):
        return container, False

    edges = container.get("edges")
    if not isinstance(edges, list):
        return container, False

    changed = False
    normalized_edges: list[Any] = []
    for edge in edges:
        if not isinstance(edge, dict):
            normalized_edges.append(edge)
            continue

        source = edge.get("source") or edge.get("from")
        target = edge.get("target") or edge.get("to")
        edge_copy = dict(edge)

        if source is not None:
            edge_copy["source"] = source
        if target is not None:
            edge_copy["target"] = target

        if "from" in edge_copy or "to" in edge_copy:
            changed = True
        edge_copy.pop("from", None)
        edge_copy.pop("to", None)

        if edge_copy != edge:
            changed = True
        normalized_edges.append(edge_copy)

    if not changed:
        return container, False

    updated = dict(container)
    updated["edges"] = normalized_edges
    return updated, True


def _resolve_tenant_id(db: Session, tenant_id: str | None, tenant_name: str | None) -> UUID | None:
    if tenant_id:
        return UUID(tenant_id)
    if tenant_name:
        tenant = db.execute(select(Tenant).where(Tenant.name == tenant_name)).scalar_one_or_none()
        if not tenant:
            raise ValueError(f"Tenant not found for name={tenant_name}")
        return tenant.id
    return None


def run(db: Session, tenant_filter: UUID | None, dry_run: bool) -> tuple[int, int]:
    flow_updates = 0
    template_updates = 0

    flow_stmt = select(Flow)
    if tenant_filter:
        flow_stmt = flow_stmt.where(Flow.tenant_id == tenant_filter)
    flows = db.execute(flow_stmt).scalars().all()

    for flow in flows:
        normalized, changed = _normalize_edges_container(flow.nodes or {})
        if changed:
            flow.nodes = normalized
            flow_updates += 1

    templates = db.execute(select(FlowTemplate)).scalars().all()
    for template in templates:
        normalized, changed = _normalize_edges_container(template.template_nodes or {})
        if changed:
            template.template_nodes = normalized
            template_updates += 1

    if not dry_run:
        db.commit()
    else:
        db.rollback()

    return flow_updates, template_updates


def main() -> None:
    parser = argparse.ArgumentParser(description="Normalize flow/template edges to source/target")
    parser.add_argument("--tenant-id", type=str, help="Tenant UUID filter")
    parser.add_argument("--tenant-name", type=str, help="Tenant name filter")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without writing")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        tenant_filter = _resolve_tenant_id(db, args.tenant_id, args.tenant_name)
        flow_updates, template_updates = run(db, tenant_filter, args.dry_run)
        mode = "DRY RUN" if args.dry_run else "APPLIED"
        print(
            f"[{mode}] normalized flows={flow_updates}, templates={template_updates}, "
            f"tenant_filter={tenant_filter}"
        )
    finally:
        db.close()


if __name__ == "__main__":
    main()
