"""
Fix seed_flow_templates.py by adding next_node_id to all nodes.

Simple approach: directly edit the Python file using string manipulation.
"""

import re


def add_next_node_id_to_node(node_str: str, next_node_id: str) -> str:
    """
    Add next_node_id to a node's config.

    Args:
        node_str: String representation of the node
        next_node_id: ID of the next node

    Returns:
        Updated node string
    """
    # Find the "config": { ... } section
    config_pattern = r'"config":\s*\{([^}]*(?:\{[^}]*\}[^}]*)*)\}'

    def replace_config(match):
        config_content = match.group(1)
        # Check if next_node_id already exists
        if '"next_node_id"' in config_content:
            return match.group(0)  # Already has it

        # Add next_node_id before the closing brace
        if config_content.strip():
            # Has other content, add comma
            return f'"config": {{{config_content},\n                            "next_node_id": "{next_node_id}"}}'
        else:
            # Empty config
            return f'"config": {{\n                            "next_node_id": "{next_node_id}"\n                        }}'

    return re.sub(config_pattern, replace_config, node_str, count=1)


def add_next_node_id_to_option(option_str: str, next_node_id: str) -> str:
    """
    Add next_node_id to a question option.

    Args:
        option_str: String representation of the option
        next_node_id: ID of the next node

    Returns:
        Updated option string
    """
    # Check if already has next_node_id
    if '"next_node_id"' in option_str:
        return option_str

    # Add before closing brace
    return option_str.replace('}', f', "next_node_id": "{next_node_id}"}}')


def process_template(template_content: str) -> str:
    """
    Process a single template section to add next_node_id.

    Args:
        template_content: String containing one template dict

    Returns:
        Updated template string
    """
    # Extract edges
    edges_match = re.search(r'"edges":\s*\[(.*?)\]', template_content, re.DOTALL)
    if not edges_match:
        return template_content  # No edges, skip

    edges_str = edges_match.group(1)

    # Parse edges
    simple_edges = []  # (from, to)
    conditional_edges = []  # (from, to, condition_value)

    # Match edge dictionaries
    for edge_match in re.finditer(r'\{[^}]+\}', edges_str):
        edge = edge_match.group(0)

        from_match = re.search(r'"from":\s*"([^"]+)"', edge)
        to_match = re.search(r'"to":\s*"([^"]+)"', edge)

        if from_match and to_match:
            from_id = from_match.group(1)
            to_id = to_match.group(1)

            # Check for condition
            cond_match = re.search(r'"condition":\s*\{[^}]*"equals":\s*"([^"]+)"', edge)
            if cond_match:
                cond_value = cond_match.group(1)
                conditional_edges.append((from_id, to_id, cond_value))
            else:
                simple_edges.append((from_id, to_id))

    # Find all nodes
    nodes_section = re.search(r'"nodes":\s*\[(.*?)\],\s*"edges"', template_content, re.DOTALL)
    if not nodes_section:
        return template_content

    nodes_str = nodes_section.group(1)
    updated_nodes_str = nodes_str

    # Process simple edges (MESSAGE, TEXT_INPUT, RAG_QUERY, WEBHOOK nodes)
    for from_id, to_id in simple_edges:
        # Find the node with this ID
        node_pattern = rf'(\{{\s*"id":\s*"{from_id}".*?(?=\{{|\Z))'

        def update_node(match):
            node = match.group(1)

            # Skip if it's a question node or terminal node
            if '"type": "question"' in node or '"type": "end"' in node or '"type": "escalate"' in node:
                return node

            return add_next_node_id_to_node(node, to_id)

        updated_nodes_str = re.sub(node_pattern, update_node, updated_nodes_str, count=1, flags=re.DOTALL)

    # Process conditional edges (QUESTION nodes)
    # Group by from_id
    question_edges = {}
    for from_id, to_id, cond_value in conditional_edges:
        if from_id not in question_edges:
            question_edges[from_id] = []
        question_edges[from_id].append((to_id, cond_value))

    for from_id, edges in question_edges.items():
        # Find the QUESTION node
        question_pattern = rf'(\{{\s*"id":\s*"{from_id}".*?"type":\s*"question".*?"options":\s*\[)(.*?)(\])'

        def update_question_options(match):
            prefix = match.group(1)
            options_str = match.group(2)
            suffix = match.group(3)

            updated_options = options_str

            # For each edge, find the matching option and add next_node_id
            for to_id, cond_value in edges:
                # Find option with this value
                option_pattern = rf'(\{{\s*"label":[^}}]*"value":\s*"{cond_value}"[^}}]*\}})'

                def update_option(opt_match):
                    return add_next_node_id_to_option(opt_match.group(1), to_id)

                updated_options = re.sub(option_pattern, update_option, updated_options, count=1)

            return prefix + updated_options + suffix

        updated_nodes_str = re.sub(question_pattern, update_question_options, updated_nodes_str, count=1, flags=re.DOTALL)

    # Replace the nodes section
    return template_content.replace(nodes_str, updated_nodes_str)


def main():
    filepath = "scripts/seed_flow_templates.py"

    print("="*70)
    print("Fixing seed_flow_templates.py")
    print("="*70)
    print()

    # Read file
    with open(filepath, 'r') as f:
        content = f.read()

    print(f"✅ Read {filepath}")

    # Extract templates section (from first { to the closing ] of get_template_data)
    # Find all template dictionaries
    templates_match = re.search(r'templates = \[(.*?)\n    \]\n\n    return', content, re.DOTALL)

    if not templates_match:
        print("❌ Could not find templates array")
        return

    templates_str = templates_match.group(1)

    print(f"✅ Found templates section")
    print()

    # Process each template
    # Split by template boundaries (each starts with # and a number)
    template_sections = re.split(r'\n\s*# \d+\.', templates_str)

    updated_sections = []
    for i, section in enumerate(template_sections):
        if not section.strip():
            continue

        # Add back the comment
        if i == 0:
            # First section doesn't need the comment added back
            updated = section
        else:
            comment_match = re.search(r'^([^\n]+)', section)
            comment = comment_match.group(1) if comment_match else ""
            updated = section

        print(f"Processing template {i if i == 0 else i}...")
        updated = process_template(updated)
        updated_sections.append(updated)

    # Reconstruct
    updated_templates_str = template_sections[0]
    for i, section in enumerate(updated_sections[1:], 1):
        updated_templates_str += f"\n        # {i}." + section

    # Replace in original content
    new_content = content.replace(templates_str, updated_templates_str)

    # Backup
    backup_path = filepath + ".backup"
    with open(backup_path, 'w') as f:
        f.write(content)

    print()
    print(f"✅ Created backup: {backup_path}")

    # Write updated file
    with open(filepath, 'w') as f:
        f.write(new_content)

    print(f"✅ Updated {filepath}")
    print()
    print("="*70)
    print("DONE!")
    print("="*70)
    print()
    print("Next: Run 'poetry run python scripts/seed_flow_templates.py' to reseed the database")


if __name__ == "__main__":
    main()
