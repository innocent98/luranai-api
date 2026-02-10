"""
Update seed_flow_templates.py to add next_node_id to all node configs

This script directly modifies the seed_flow_templates.py file to fix the
architectural mismatch between template format and flow engine execution.
"""

import json
import ast
import re


def convert_template_edges_to_next_node_id(template_data: dict) -> dict:
    """
    Convert a single template from edges format to next_node_id format.

    Args:
        template_data: Template dict with template_nodes containing nodes and edges

    Returns:
        Updated template dict with next_node_id in node configs
    """
    template_nodes = template_data.get("template_nodes", {})
    nodes = template_nodes.get("nodes", [])
    edges = template_nodes.get("edges", [])

    # Build edge map
    edge_map = {}  # {from_node_id: to_node_id}
    conditional_edges = {}  # {from_node_id: [{condition, next_node_id}]}

    for edge in edges:
        from_node = edge.get("from")
        to_node = edge.get("to")
        condition = edge.get("condition")

        if condition:
            if from_node not in conditional_edges:
                conditional_edges[from_node] = []
            conditional_edges[from_node].append({
                "condition": condition,
                "next_node_id": to_node
            })
        else:
            edge_map[from_node] = to_node

    # Update nodes
    updated_nodes = []
    for node in nodes:
        node_id = node.get("id")
        node_type = node.get("type")
        config = node.get("config", {})

        if node_type == "question":
            # Add next_node_id to each option
            options = config.get("options", [])
            node_edges = conditional_edges.get(node_id, [])

            updated_options = []
            for option in options:
                option_value = option.get("value")
                updated_option = option.copy()

                # Find matching edge
                for edge in node_edges:
                    if edge.get("condition") == option_value:
                        updated_option["next_node_id"] = edge["next_node_id"]
                        break

                updated_options.append(updated_option)

            config["options"] = updated_options
        else:
            # Add next_node_id to config for non-terminal nodes
            if node_type not in ["end", "escalate"] and node_id in edge_map:
                config["next_node_id"] = edge_map[node_id]

        updated_node = {
            "id": node.get("id"),
            "type": node.get("type"),
            "config": config,
            "position": node.get("position", {})
        }
        updated_nodes.append(updated_node)

    # Update template_nodes
    template_data["template_nodes"]["nodes"] = updated_nodes
    return template_data


def main():
    """Main execution - read, fix, and write seed file."""
    seed_file = "scripts/seed_flow_templates.py"

    print("="*70)
    print("Updating seed_flow_templates.py")
    print("="*70)
    print()

    # Read the file
    with open(seed_file, 'r') as f:
        content = f.read()

    print(f"✅ Read {seed_file}")

    # Extract TEMPLATES array using regex
    # Find the TEMPLATES = [ ... ] section
    match = re.search(r'TEMPLATES\s*=\s*\[(.*?)\n\]', content, re.DOTALL)

    if not match:
        print("❌ Could not find TEMPLATES array in seed file")
        return

    templates_str = '[' + match.group(1) + '\n]'

    # Parse templates (this is tricky because it's Python code, not JSON)
    # We'll use ast.literal_eval to safely parse the Python data structure
    try:
        templates = ast.literal_eval(templates_str)
    except Exception as e:
        print(f"❌ Could not parse TEMPLATES: {e}")
        return

    print(f"✅ Parsed {len(templates)} templates")
    print()

    # Fix each template
    fixed_templates = []
    for i, template in enumerate(templates):
        template_name = template.get("name", f"Template {i+1}")
        print(f"Fixing template {i+1}/{len(templates)}: {template_name}")

        fixed_template = convert_template_edges_to_next_node_id(template)
        fixed_templates.append(fixed_template)

    print()
    print("✅ All templates fixed")
    print()

    # Convert back to Python code format
    fixed_templates_str = "TEMPLATES = " + json.dumps(fixed_templates, indent=4)

    # Replace in original content
    new_content = re.sub(
        r'TEMPLATES\s*=\s*\[.*?\n\]',
        fixed_templates_str,
        content,
        count=1,
        flags=re.DOTALL
    )

    # Write back to file
    backup_file = seed_file + ".backup"
    with open(backup_file, 'w') as f:
        f.write(content)
    print(f"✅ Created backup: {backup_file}")

    with open(seed_file, 'w') as f:
        f.write(new_content)
    print(f"✅ Updated {seed_file}")

    print()
    print("="*70)
    print("✅ DONE - Seed file updated successfully!")
    print("="*70)
    print()
    print("Next steps:")
    print("1. Review the changes in scripts/seed_flow_templates.py")
    print("2. Run: poetry run python scripts/seed_flow_templates.py")
    print("3. Test the FAQ Handler flow again")


if __name__ == "__main__":
    main()
