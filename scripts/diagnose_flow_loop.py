"""
Diagnose flow loop issues

This script checks for common flow configuration problems that cause loops.

Usage:
    python scripts/diagnose_flow_loop.py --flow-name "FAQ Handler"
"""

import argparse
import sys
from pathlib import Path
import json

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy.orm import Session
from app.db.session import SessionLocal
from app.db.models.flow import Flow
from sqlalchemy import select


def diagnose_flow(db: Session, flow_name: str):
    """Diagnose flow configuration for loop issues."""

    # Find flow
    stmt = select(Flow).where(Flow.name.ilike(f'%{flow_name}%'))
    flows = db.execute(stmt).scalars().all()

    if not flows:
        print(f"❌ No flows found matching '{flow_name}'")
        print("\nActive flows:")
        all_flows = db.query(Flow).filter(Flow.is_active == True).all()
        for f in all_flows:
            print(f"  - {f.name} (ID: {f.id})")
        return

    for flow in flows:
        print("\n" + "="*70)
        print(f"🔍 Analyzing Flow: {flow.name}")
        print(f"   ID: {flow.id}")
        print(f"   Status: {flow.status}, Active: {flow.is_active}")
        print("="*70 + "\n")

        if not flow.nodes:
            print("❌ ERROR: No nodes configured in this flow!")
            continue

        nodes = flow.nodes.get('nodes', [])
        start_node_id = flow.nodes.get('start_node_id')

        print(f"Start Node ID: {start_node_id}")
        print(f"Total Nodes: {len(nodes)}\n")

        # Check for issues
        issues = []
        warnings = []

        # Issue 1: No start node
        if not start_node_id:
            issues.append("❌ No start_node_id configured!")

        # Issue 2: Start node doesn't exist
        start_node = next((n for n in nodes if n.get('id') == start_node_id), None)
        if start_node_id and not start_node:
            issues.append(f"❌ Start node '{start_node_id}' not found in nodes list!")

        # Build node map
        node_map = {n.get('id'): n for n in nodes}

        # Issue 3: Self-referencing nodes (loop to self)
        for node in nodes:
            node_id = node.get('id')
            node_type = node.get('type')
            config = node.get('config', {})
            next_node_id = config.get('next_node_id')

            # Check for self-reference
            if next_node_id == node_id:
                issues.append(
                    f"❌ LOOP DETECTED: Node '{node_id}' (type: {node_type}) points to itself!"
                )

            # Check for MESSAGE nodes without next_node_id
            if node_type == 'message' and not next_node_id:
                warnings.append(
                    f"⚠️  Node '{node_id}' is a MESSAGE node with no next_node_id (flow will end)"
                )

            # Check if next_node_id exists
            if next_node_id and next_node_id not in node_map:
                issues.append(
                    f"❌ Node '{node_id}' references non-existent node: '{next_node_id}'"
                )

        # Issue 4: Two-node loops (A -> B -> A)
        for node in nodes:
            node_id = node.get('id')
            config = node.get('config', {})
            next_node_id = config.get('next_node_id')

            if next_node_id and next_node_id in node_map:
                next_node = node_map[next_node_id]
                next_config = next_node.get('config', {})
                next_next_id = next_config.get('next_node_id')

                if next_next_id == node_id:
                    warnings.append(
                        f"⚠️  POTENTIAL LOOP: Node '{node_id}' → '{next_node_id}' → back to '{node_id}'"
                    )

        # Issue 5: Check start node configuration
        if start_node:
            print("📍 Start Node Configuration:")
            print(f"   ID: {start_node.get('id')}")
            print(f"   Type: {start_node.get('type')}")
            start_config = start_node.get('config', {})
            print(f"   Message: {start_config.get('message', 'N/A')[:80]}...")
            print(f"   Next Node: {start_config.get('next_node_id', 'NONE')}")

            next_id = start_config.get('next_node_id')
            if next_id == start_node.get('id'):
                print("   ❌ START NODE POINTS TO ITSELF - THIS CAUSES THE LOOP!")
            elif not next_id:
                print("   ⚠️  No next node - flow will end after first message")
            elif next_id in node_map:
                next_node = node_map[next_id]
                print(f"\n📍 Next Node After Start:")
                print(f"   ID: {next_node.get('id')}")
                print(f"   Type: {next_node.get('type')}")
                next_next_config = next_node.get('config', {})
                if next_node.get('type') == 'message':
                    print(f"   Message: {next_next_config.get('message', 'N/A')[:80]}...")
                    print(f"   Next Node: {next_next_config.get('next_node_id', 'NONE')}")
                elif next_node.get('type') == 'question':
                    print(f"   Question: {next_next_config.get('question', 'N/A')[:80]}...")
                    options = next_next_config.get('options', [])
                    print(f"   Options: {len(options)} choices")
            else:
                print(f"   ❌ Next node '{next_id}' doesn't exist!")
            print()

        # Print summary
        print("\n" + "="*70)
        print("DIAGNOSIS SUMMARY")
        print("="*70)

        if issues:
            print(f"\n❌ {len(issues)} CRITICAL ISSUE(S) FOUND:\n")
            for issue in issues:
                print(f"   {issue}")

        if warnings:
            print(f"\n⚠️  {len(warnings)} WARNING(S):\n")
            for warning in warnings:
                print(f"   {warning}")

        if not issues and not warnings:
            print("\n✅ No obvious loop issues detected!")
            print("\nHowever, if you're still experiencing loops, the issue might be:")
            print("  1. The flow execution isn't advancing properly")
            print("  2. The user input isn't being passed to the next node")
            print("  3. There's a circular path through multiple nodes")

        print("\n" + "="*70)

        # Show full flow structure
        print("\nFULL FLOW STRUCTURE:")
        print("="*70)
        for i, node in enumerate(nodes):
            node_id = node.get('id')
            node_type = node.get('type')
            config = node.get('config', {})
            next_node_id = config.get('next_node_id')

            marker = "🔴" if node_id == start_node_id else "  "

            print(f"\n{marker} Node {i+1}: {node_id}")
            print(f"   Type: {node_type}")

            if node_type == 'message':
                msg = config.get('message', 'N/A')
                print(f"   Message: {msg[:100]}{'...' if len(msg) > 100 else ''}")
            elif node_type == 'question':
                q = config.get('question', 'N/A')
                print(f"   Question: {q[:100]}{'...' if len(q) > 100 else ''}")
                options = config.get('options', [])
                print(f"   Options:")
                for opt in options:
                    opt_label = opt.get('label', 'N/A')
                    opt_next = opt.get('next_node_id', 'NONE')
                    print(f"      - '{opt_label}' → {opt_next}")

            if next_node_id:
                print(f"   Next: {next_node_id}")
            else:
                print(f"   Next: [END]")

        print("\n" + "="*70)


def main():
    """Main execution."""
    parser = argparse.ArgumentParser(
        description="Diagnose flow loop issues"
    )
    parser.add_argument(
        "--flow-name",
        help="Flow name to diagnose (partial match)",
        default="FAQ"
    )
    parser.add_argument(
        "--flow-id",
        help="Flow ID to diagnose",
        default=None
    )

    args = parser.parse_args()

    db = SessionLocal()

    try:
        if args.flow_id:
            from uuid import UUID
            flow = db.query(Flow).filter(Flow.id == UUID(args.flow_id)).first()
            if flow:
                diagnose_flow(db, flow.name)
            else:
                print(f"❌ Flow with ID {args.flow_id} not found")
        else:
            diagnose_flow(db, args.flow_name)

    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    main()
