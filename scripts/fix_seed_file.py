"""
Fix seed_flow_templates.py by adding next_node_id to all nodes based on edges.

This is a one-time fix script to resolve the architectural mismatch.
"""

import re
import json


def parse_templates_from_file(filepath: str) -> tuple[str, list]:
    """
    Read the seed file and extract templates.

    Returns:
        (full_content, templates_list)
    """
    with open(filepath, 'r') as f:
        content = f.read()

    # Execute the file to get TEMPLATES variable
    import os
    global_vars = {'__file__': os.path.abspath(filepath)}
    local_vars = {}
    exec(content, global_vars, local_vars)
    templates = local_vars.get('TEMPLATES', [])

    return content, templates


def fix_template(template: dict) -> dict:
    """Fix a single template by adding next_node_id to nodes."""
    template_nodes = template.get("template_nodes", {})
    nodes = template_nodes.get("nodes", [])
    edges = template_nodes.get("edges", [])

    # Build edge maps
    simple_edges = {}  # {from: to}
    conditional_edges = {}  # {from: [{condition, to}]}

    for edge in edges:
        from_node = edge.get("from")
        to_node = edge.get("to")
        condition = edge.get("condition")

        if condition:
            if from_node not in conditional_edges:
                conditional_edges[from_node] = []
            conditional_edges[from_node].append({
                "condition": condition,
                "to": to_node
            })
        else:
            simple_edges[from_node] = to_node

    # Update nodes
    for node in nodes:
        node_id = node.get("id")
        node_type = node.get("type")
        config = node.get("config", {})

        # Skip if already has next_node_id
        if node_type == "question":
            # Check options
            options = config.get("options", [])
            if any("next_node_id" in opt for opt in options):
                continue  # Already fixed

            # Add next_node_id to options
            if node_id in conditional_edges:
                for option in options:
                    option_value = option.get("value")
                    # Find matching edge
                    for edge in conditional_edges[node_id]:
                        cond = edge.get("condition", {})
                        if cond.get("equals") == option_value:
                            option["next_node_id"] = edge["to"]
                            break
        else:
            # Skip terminal nodes
            if node_type in ["end", "escalate"]:
                continue

            # Skip if already has next_node_id
            if "next_node_id" in config:
                continue

            # Add next_node_id from simple edges
            if node_id in simple_edges:
                config["next_node_id"] = simple_edges[node_id]

    return template


def generate_template_code(templates: list) -> str:
    """Generate Python code for TEMPLATES list."""
    return "TEMPLATES = " + json.dumps(templates, indent=4)


def main():
    filepath = "scripts/seed_flow_templates.py"

    print("="*70)
    print("Fixing seed_flow_templates.py")
    print("="*70)
    print()

    # Parse file
    print(f"Reading {filepath}...")
    content, templates = parse_templates_from_file(filepath)
    print(f"✅ Found {len(templates)} templates")
    print()

    # Fix each template
    print("Fixing templates...")
    for i, template in enumerate(templates):
        name = template.get("name", f"Template {i+1}")
        print(f"  {i+1}. {name}")
        fix_template(template)

    print()
    print("✅ All templates fixed in memory")
    print()

    # Generate new code
    new_templates_code = generate_template_code(templates)

    # Replace TEMPLATES in content
    new_content = re.sub(
        r'TEMPLATES\s*=\s*\[.*?\n\](?=\s*\n\n|\s*$)',
        new_templates_code,
        content,
        count=1,
        flags=re.DOTALL
    )

    # Backup original
    backup_path = filepath + ".backup"
    with open(backup_path, 'w') as f:
        f.write(content)
    print(f"✅ Created backup: {backup_path}")

    # Write fixed version
    with open(filepath, 'w') as f:
        f.write(new_content)
    print(f"✅ Updated {filepath}")

    print()
    print("="*70)
    print("DONE!")
    print("="*70)
    print()
    print("Next steps:")
    print("1. Run: poetry run python scripts/seed_flow_templates.py")
    print("2. Test the FAQ Handler flow")
    print()
    print("To verify the fix:")
    print("  - Check that each MESSAGE/TEXT_INPUT/RAG_QUERY node has 'next_node_id' in config")
    print("  - Check that QUESTION node options have 'next_node_id' field")


if __name__ == "__main__":
    main()
