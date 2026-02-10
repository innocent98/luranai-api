"""
Add enhanced validation to flow_service.py to prevent loop bugs.

This script shows the code that needs to be added to app/services/flow_service.py
"""

VALIDATION_CODE = '''
    # NEW: Validate next_node_id exists for non-terminal nodes (prevents loop bugs)
    TERMINAL_NODE_TYPES = {"end", "escalate"}
    BRANCHING_NODE_TYPES = {"question", "condition"}

    for node in node_list:
        node_id = node.get("id")
        node_type = node.get("type")
        config = node.get("config", {})

        # Skip terminal nodes (they don't need next_node_id)
        if node_type in TERMINAL_NODE_TYPES:
            continue

        # Validate QUESTION nodes
        if node_type == "question":
            options = config.get("options", [])

            # Check if question has config-level next_node_id
            has_config_next = "next_node_id" in config

            # Check if ALL options have next_node_id
            all_options_have_next = all(
                "next_node_id" in option
                for option in options
            )

            # Question must have EITHER:
            # 1. Config-level next_node_id (all options go to same node), OR
            # 2. All options have their own next_node_id
            if not has_config_next and not all_options_have_next:
                raise ValueError(
                    f"QUESTION node '{node_id}' must have either:\\n"
                    f"  - 'next_node_id' in config (all options go to same node), OR\\n"
                    f"  - 'next_node_id' in EVERY option (conditional branching)\\n"
                    f"Without this, the flow will loop indefinitely on this node."
                )

        # Validate CONDITION nodes
        elif node_type == "condition":
            # Condition nodes must have default_node_id
            if "default_node_id" not in config:
                raise ValueError(
                    f"CONDITION node '{node_id}' must have 'default_node_id' in config"
                )

            # All conditions must have next_node_id
            conditions = config.get("conditions", [])
            for idx, condition in enumerate(conditions):
                if "next_node_id" not in condition:
                    raise ValueError(
                        f"CONDITION node '{node_id}' condition #{idx} must have 'next_node_id'"
                    )

        # Validate linear flow nodes (MESSAGE, TEXT_INPUT, RAG_QUERY, WEBHOOK, etc.)
        else:
            if "next_node_id" not in config:
                raise ValueError(
                    f"{node_type.upper()} node '{node_id}' must have 'next_node_id' in config.\\n"
                    f"Without this, the flow will loop indefinitely on this node.\\n"
                    f"Tip: Set 'next_node_id' to the ID of the next node in the flow."
                )
'''

def main():
    print("="*70)
    print("Enhanced Flow Validation Code")
    print("="*70)
    print()
    print("Add this code to app/services/flow_service.py")
    print("Location: Inside _validate_nodes() method, after line 348")
    print()
    print("="*70)
    print()
    print(VALIDATION_CODE)
    print()
    print("="*70)
    print()
    print("After adding this code:")
    print("1. Restart your API server")
    print("2. Try creating a flow without next_node_id (should fail)")
    print("3. Try creating a flow with next_node_id (should succeed)")
    print()
    print("See docs/PREVENT_FLOW_LOOP_BUG.md for full implementation guide")

if __name__ == "__main__":
    main()
