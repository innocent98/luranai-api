"""
Fix common flow loop issues

This script automatically fixes common flow configuration problems.

Usage:
    python scripts/fix_flow_loop.py --flow-id <flow-uuid>

    # Or by name
    python scripts/fix_flow_loop.py --flow-name "FAQ Handler"
"""

import argparse
import sys
from pathlib import Path
import json
from uuid import UUID

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy.orm import Session
from app.db.session import SessionLocal
from app.db.models.flow import Flow
from sqlalchemy import select


def fix_self_referencing_nodes(nodes: list) -> tuple[list, list]:
    """
    Fix nodes that point to themselves.

    Returns:
        (fixed_nodes, list_of_fixes)
    """
    fixes = []
    fixed_nodes = []

    for node in nodes:
        node_id = node.get('id')
        config = node.get('config', {})
        next_node_id = config.get('next_node_id')

        # Check for self-reference
        if next_node_id == node_id:
            # Remove self-reference (let flow end)
            config_copy = config.copy()
            del config_copy['next_node_id']

            node_copy = node.copy()
            node_copy['config'] = config_copy

            fixed_nodes.append(node_copy)
            fixes.append(f"Removed self-reference from node '{node_id}'")
        else:
            fixed_nodes.append(node)

    return fixed_nodes, fixes


def suggest_question_node_addition(nodes: list, start_node_id: str) -> dict:
    """
    Suggest adding a QUESTION node after MESSAGE-only start.

    Returns:
        Suggested question node config
    """
    return {
        "id": "question_main",
        "type": "question",
        "config": {
            "question": "What would you like to know?",
            "options": [
                {
                    "label": "General information",
                    "value": "general",
                    "next_node_id": "end"
                },
                {
                    "label": "Something else",
                    "value": "other",
                    "next_node_id": "end"
                }
            ]
        },
        "position": {"x": 200, "y": 100}
    }


def fix_flow(db: Session, flow_id: UUID, dry_run: bool = True):
    """
    Fix flow configuration issues.

    Args:
        db: Database session
        flow_id: Flow UUID
        dry_run: If True, only show what would be fixed without saving
    """
    # Get flow
    flow = db.query(Flow).filter(Flow.id == flow_id).first()

    if not flow:
        print(f"❌ Flow with ID {flow_id} not found")
        return

    print("\n" + "="*70)
    print(f"🔧 Analyzing Flow: {flow.name}")
    print(f"   ID: {flow.id}")
    print("="*70 + "\n")

    if not flow.nodes:
        print("❌ ERROR: No nodes configured in this flow!")
        return

    nodes = flow.nodes.get('nodes', [])
    start_node_id = flow.nodes.get('start_node_id')
    edges = flow.nodes.get('edges', [])

    print(f"Current configuration:")
    print(f"  - Start Node: {start_node_id}")
    print(f"  - Total Nodes: {len(nodes)}")
    print(f"  - Edges: {len(edges)}\n")

    # Apply fixes
    all_fixes = []
    suggestions = []

    # Fix 1: Self-referencing nodes
    fixed_nodes, fixes = fix_self_referencing_nodes(nodes)
    all_fixes.extend(fixes)

    # Fix 2: Check if start node needs next_node_id
    start_node = next((n for n in fixed_nodes if n.get('id') == start_node_id), None)
    if start_node:
        config = start_node.get('config', {})
        node_type = start_node.get('type')
        next_node_id = config.get('next_node_id')

        if node_type == 'message' and not next_node_id:
            # Start message node has no next node - suggest adding question
            suggestions.append(
                "Start MESSAGE node has no next_node_id. "
                "Suggest adding a QUESTION node for interaction."
            )

            # Check if there's already a question node
            question_nodes = [n for n in fixed_nodes if n.get('type') == 'question']

            if question_nodes:
                # Use existing question node
                first_question = question_nodes[0]
                question_id = first_question.get('id')

                # Update start node to point to question
                for node in fixed_nodes:
                    if node.get('id') == start_node_id:
                        node['config']['next_node_id'] = question_id
                        all_fixes.append(
                            f"Updated start node to point to existing question node '{question_id}'"
                        )
                        break
            else:
                # Suggest adding new question node
                suggested_node = suggest_question_node_addition(fixed_nodes, start_node_id)
                suggestions.append(
                    f"\nSuggested new QUESTION node:\n{json.dumps(suggested_node, indent=2)}"
                )

    # Print results
    print("\n" + "="*70)
    print("FIXES APPLIED" if not dry_run else "FIXES TO BE APPLIED")
    print("="*70 + "\n")

    if all_fixes:
        for fix in all_fixes:
            print(f"✅ {fix}")
    else:
        print("No automatic fixes needed.")

    if suggestions:
        print("\n" + "="*70)
        print("SUGGESTIONS")
        print("="*70 + "\n")
        for suggestion in suggestions:
            print(f"💡 {suggestion}")

    # Show what changed
    if all_fixes:
        print("\n" + "="*70)
        print("BEFORE vs AFTER")
        print("="*70 + "\n")

        for node in nodes:
            node_id = node.get('id')
            fixed_node = next((n for n in fixed_nodes if n.get('id') == node_id), None)

            if fixed_node and node != fixed_node:
                print(f"Node: {node_id}")
                print(f"  BEFORE: {json.dumps(node.get('config', {}), indent=4)}")
                print(f"  AFTER:  {json.dumps(fixed_node.get('config', {}), indent=4)}")
                print()

    # Save changes if not dry run
    if not dry_run and (all_fixes or suggestions):
        print("\n" + "="*70)
        print("SAVING CHANGES")
        print("="*70 + "\n")

        # Update flow nodes
        flow.nodes['nodes'] = fixed_nodes
        db.commit()

        print("✅ Changes saved to database!")
        print(f"\nUpdated flow: {flow.name} (ID: {flow.id})")
    elif dry_run:
        print("\n" + "="*70)
        print("DRY RUN - No changes saved")
        print("="*70)
        print("\nTo apply these fixes, run with --apply flag:")
        print(f"python scripts/fix_flow_loop.py --flow-id {flow_id} --apply")

    print()


def main():
    """Main execution."""
    parser = argparse.ArgumentParser(
        description="Fix flow loop issues"
    )
    parser.add_argument(
        "--flow-id",
        help="Flow ID to fix",
        default=None
    )
    parser.add_argument(
        "--flow-name",
        help="Flow name to fix (partial match)",
        default=None
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply fixes (default is dry-run)"
    )

    args = parser.parse_args()

    if not args.flow_id and not args.flow_name:
        print("❌ Error: Provide either --flow-id or --flow-name")
        parser.print_help()
        sys.exit(1)

    db = SessionLocal()

    try:
        if args.flow_id:
            flow_id = UUID(args.flow_id)
            fix_flow(db, flow_id, dry_run=not args.apply)
        else:
            # Find flow by name
            stmt = select(Flow).where(Flow.name.ilike(f'%{args.flow_name}%'))
            flows = db.execute(stmt).scalars().all()

            if not flows:
                print(f"❌ No flows found matching '{args.flow_name}'")
                sys.exit(1)
            elif len(flows) > 1:
                print(f"⚠️  Multiple flows found matching '{args.flow_name}':")
                for f in flows:
                    print(f"   - {f.name} (ID: {f.id})")
                print("\nPlease use --flow-id to specify which one to fix")
                sys.exit(1)
            else:
                fix_flow(db, flows[0].id, dry_run=not args.apply)

    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    main()
