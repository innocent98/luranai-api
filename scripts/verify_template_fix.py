"""
Verify that flow templates have been fixed (have next_node_id in configs).

This script checks the seed file to ensure all nodes have proper next_node_id.
"""

import re
import sys


def check_template_fix(template_content: str, template_name: str) -> dict:
    """
    Check if a template has been fixed.

    Returns:
        {
            'fixed': bool,
            'issues': [list of issue descriptions],
            'node_count': int,
            'fixed_nodes': int
        }
    """
    issues = []
    total_nodes = 0
    fixed_nodes = 0

    # Extract nodes section
    nodes_match = re.search(r'"nodes":\s*\[(.*?)\],\s*"edges"', template_content, re.DOTALL)
    if not nodes_match:
        return {'fixed': False, 'issues': ['Could not find nodes array'], 'node_count': 0, 'fixed_nodes': 0}

    nodes_str = nodes_match.group(1)

    # Find all node definitions
    node_pattern = r'\{[^}]*"id":\s*"([^"]+)"[^}]*"type":\s*"([^"]+)".*?"config":\s*\{(.*?)\}'

    for match in re.finditer(node_pattern, nodes_str, re.DOTALL):
        node_id = match.group(1)
        node_type = match.group(2)
        config_str = match.group(3)

        total_nodes += 1

        # Skip terminal nodes
        if node_type in ['end', 'escalate']:
            fixed_nodes += 1
            continue

        # Check if node has next_node_id
        if node_type == 'question':
            # QUESTION nodes need next_node_id in options
            has_next_in_options = '"next_node_id"' in config_str and '"options"' in config_str
            if has_next_in_options:
                fixed_nodes += 1
            else:
                issues.append(f"QUESTION node '{node_id}' missing next_node_id in options")
        else:
            # Other nodes (MESSAGE, TEXT_INPUT, RAG_QUERY, WEBHOOK) need next_node_id in config
            if '"next_node_id"' in config_str:
                fixed_nodes += 1
            else:
                issues.append(f"{node_type.upper()} node '{node_id}' missing next_node_id in config")

    is_fixed = len(issues) == 0

    return {
        'fixed': is_fixed,
        'issues': issues,
        'node_count': total_nodes,
        'fixed_nodes': fixed_nodes
    }


def main():
    filepath = "scripts/seed_flow_templates.py"

    print("="*70)
    print("Flow Template Fix Verification")
    print("="*70)
    print()

    # Read file
    with open(filepath, 'r') as f:
        content = f.read()

    print(f"✅ Read {filepath}")
    print()

    # Find all templates
    templates_match = re.search(r'templates = \[(.*?)\n    \]\n\n    return', content, re.DOTALL)
    if not templates_match:
        print("❌ Could not find templates array")
        sys.exit(1)

    templates_str = templates_match.group(1)

    # Extract template names and their content
    template_pattern = r'# \d+\. ([^\n]+).*?\{.*?"name":\s*"([^"]+)".*?\}(?=\n\s*# \d+\.|$)'

    # Split into individual templates
    template_sections = re.split(r'(?=\n\s*# \d+\.)', templates_str)

    results = []

    for section in template_sections:
        if not section.strip():
            continue

        # Extract template name
        name_match = re.search(r'"name":\s*"([^"]+)"', section)
        if not name_match:
            continue

        template_name = name_match.group(1)

        # Check if fixed
        result = check_template_fix(section, template_name)
        results.append({
            'name': template_name,
            **result
        })

    # Print results
    print("="*70)
    print("VERIFICATION RESULTS")
    print("="*70)
    print()

    fixed_count = 0
    for i, result in enumerate(results):
        name = result['name']
        is_fixed = result['fixed']
        node_count = result['node_count']
        fixed_nodes = result['fixed_nodes']
        issues = result['issues']

        status = "✅ FIXED" if is_fixed else "❌ NEEDS FIX"
        print(f"{i+1}. {name}")
        print(f"   Status: {status}")
        print(f"   Nodes: {fixed_nodes}/{node_count} fixed")

        if issues:
            print(f"   Issues:")
            for issue in issues:
                print(f"      - {issue}")

        print()

        if is_fixed:
            fixed_count += 1

    # Summary
    print("="*70)
    print("SUMMARY")
    print("="*70)
    print(f"Total templates: {len(results)}")
    print(f"Fixed: {fixed_count}")
    print(f"Needs fixing: {len(results) - fixed_count}")
    print()

    if fixed_count == len(results):
        print("🎉 All templates are fixed and ready to deploy!")
    else:
        print(f"⚠️  {len(results) - fixed_count} template(s) still need fixing")
        print()
        print("To fix remaining templates:")
        print("1. See docs/FLOW_TEMPLATE_FIX_SUMMARY.md for detailed instructions")
        print("2. Or run: python3 scripts/fix_seed_file_simple.py (automated)")


if __name__ == "__main__":
    main()
