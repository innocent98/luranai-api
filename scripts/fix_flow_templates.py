"""
Fix flow templates by converting edges array to next_node_id in node configs

This script fixes the architectural mismatch between:
- Template format: Uses separate "edges" array for connections
- Flow engine: Expects "next_node_id" in each node's config

Usage:
    python scripts/fix_flow_templates.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy.orm import Session
from app.db.session import SessionLocal
from app.db.models.flow import FlowTemplate
from sqlalchemy import select


def convert_edges_to_next_node_id(template_nodes: dict) -> dict:
    """
    Convert edges array to next_node_id in node configs.

    Args:
        template_nodes: Template nodes dict with "nodes" and "edges" arrays

    Returns:
        Updated template_nodes dict with next_node_id in node configs
    """
    nodes = template_nodes.get("nodes", [])
    edges = template_nodes.get("edges", [])

    # Build edge map: {from_node_id: to_node_id}
    edge_map = {}
    conditional_edges = {}  # For edges with conditions (QUESTION nodes)

    for edge in edges:
        from_node = edge.get("from")
        to_node = edge.get("to")
        condition = edge.get("condition")

        if condition:
            # This is a conditional edge (from QUESTION node option)
            if from_node not in conditional_edges:
                conditional_edges[from_node] = []
            conditional_edges[from_node].append({
                "condition": condition,
                "next_node_id": to_node
            })
        else:
            # Simple edge
            edge_map[from_node] = to_node

    # Update nodes with next_node_id
    updated_nodes = []
    for node in nodes:
        node_id = node.get("id")
        node_type = node.get("type")
        config = node.get("config", {})

        if node_type == "question":
            # For QUESTION nodes, add next_node_id to each option
            options = config.get("options", [])

            # Get conditional edges for this node
            node_edges = conditional_edges.get(node_id, [])

            # Match edges to options by condition/value
            updated_options = []
            for option in options:
                option_value = option.get("value")

                # Find matching edge
                matching_edge = None
                for edge in node_edges:
                    if edge.get("condition") == option_value:
                        matching_edge = edge
                        break

                # Add next_node_id to option
                updated_option = option.copy()
                if matching_edge:
                    updated_option["next_node_id"] = matching_edge["next_node_id"]
                updated_options.append(updated_option)

            config["options"] = updated_options
        else:
            # For other node types (MESSAGE, TEXT_INPUT, etc.), add next_node_id to config
            if node_id in edge_map:
                config["next_node_id"] = edge_map[node_id]

        updated_node = node.copy()
        updated_node["config"] = config
        updated_nodes.append(updated_node)

    # Return updated template_nodes
    return {
        "start_node_id": template_nodes.get("start_node_id"),
        "nodes": updated_nodes,
        "edges": edges  # Keep edges for backward compatibility
    }


def fix_template(db: Session, template: FlowTemplate) -> bool:
    """
    Fix a single template by converting edges to next_node_id.

    Args:
        db: Database session
        template: FlowTemplate to fix

    Returns:
        True if template was fixed, False if no changes needed
    """
    print(f"\n{'='*70}")
    print(f"Fixing Template: {template.name}")
    print(f"Category: {template.category}")
    print(f"{'='*70}")

    template_nodes = template.template_nodes
    if not template_nodes:
        print("❌ No template_nodes found - skipping")
        return False

    nodes = template_nodes.get("nodes", [])
    edges = template_nodes.get("edges", [])

    print(f"Current structure:")
    print(f"  - Nodes: {len(nodes)}")
    print(f"  - Edges: {len(edges)}")

    # Check if any node is missing next_node_id
    needs_fix = False
    for node in nodes:
        node_id = node.get("id")
        node_type = node.get("type")
        config = node.get("config", {})

        if node_type == "question":
            # Check if options have next_node_id
            options = config.get("options", [])
            for option in options:
                if "next_node_id" not in option:
                    needs_fix = True
                    print(f"  ⚠️  QUESTION node '{node_id}' has option without next_node_id")
                    break
        elif node_type not in ["end", "escalate"]:
            # MESSAGE, TEXT_INPUT, RAG_QUERY, WEBHOOK should have next_node_id
            if "next_node_id" not in config:
                needs_fix = True
                print(f"  ⚠️  {node_type.upper()} node '{node_id}' missing next_node_id")

    if not needs_fix:
        print("✅ Template already has next_node_id in all nodes - no fix needed")
        return False

    # Apply fix
    print("\nApplying fix...")
    updated_template_nodes = convert_edges_to_next_node_id(template_nodes)

    # Update template
    template.template_nodes = updated_template_nodes

    # Verify fix
    print("\nVerifying fix...")
    fixed_nodes = updated_template_nodes.get("nodes", [])
    fix_success = True

    for node in fixed_nodes:
        node_id = node.get("id")
        node_type = node.get("type")
        config = node.get("config", {})

        if node_type == "question":
            options = config.get("options", [])
            for option in options:
                if "next_node_id" not in option:
                    print(f"  ❌ QUESTION node '{node_id}' still has option without next_node_id")
                    fix_success = False
        elif node_type not in ["end", "escalate"]:
            if "next_node_id" not in config:
                print(f"  ❌ {node_type.upper()} node '{node_id}' still missing next_node_id")
                fix_success = False

    if fix_success:
        print("✅ Fix verified - all nodes now have next_node_id")
        return True
    else:
        print("❌ Fix incomplete - some nodes still missing next_node_id")
        return False


def main():
    """Main execution."""
    print("\n" + "="*70)
    print("Flow Template Fix Script")
    print("="*70)
    print("\nThis script converts flow templates from edge-based to next_node_id-based format.")
    print("This fixes the loop bug where flows repeat the same message.\n")

    db = SessionLocal()

    try:
        # Get all templates
        stmt = select(FlowTemplate)
        templates = db.execute(stmt).scalars().all()

        if not templates:
            print("❌ No flow templates found in database")
            sys.exit(1)

        print(f"Found {len(templates)} flow templates\n")

        fixed_count = 0
        skipped_count = 0

        for template in templates:
            was_fixed = fix_template(db, template)
            if was_fixed:
                fixed_count += 1
            else:
                skipped_count += 1

        # Commit all changes
        if fixed_count > 0:
            db.commit()
            print("\n" + "="*70)
            print("SUMMARY")
            print("="*70)
            print(f"✅ Fixed: {fixed_count} templates")
            print(f"⏭️  Skipped: {skipped_count} templates (already correct)")
            print(f"\n✅ All changes committed to database!")
        else:
            print("\n" + "="*70)
            print("SUMMARY")
            print("="*70)
            print(f"✅ All {len(templates)} templates are already correct!")
            print(f"No changes needed.")

    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    main()
