# Preventing Flow Loop Bug in User-Created Flows

**Issue:** Flow templates had missing `next_node_id` causing infinite loops
**Risk:** Users creating flows via dashboard could create the same bug
**Solution:** Enhanced validation + user guidance

---

## Current Validation Gap

### What's Already Validated ✅

The `_validate_nodes()` method in `app/services/flow_service.py:244-348` checks:
- ✅ Nodes must be a dictionary
- ✅ Flow must have at least one node
- ✅ Flow must have a start_node_id
- ✅ No duplicate node IDs
- ✅ Each node must have a type
- ✅ **IF** `next_node_id` exists → must point to valid node
- ✅ QUESTION node options with `next_node_id` → must point to valid node
- ✅ CONDITION node branches → must point to valid nodes

### What's NOT Validated ❌

- ❌ MESSAGE nodes **MUST** have `next_node_id` in config
- ❌ TEXT_INPUT nodes **MUST** have `next_node_id` in config
- ❌ RAG_QUERY nodes **MUST** have `next_node_id` in config
- ❌ WEBHOOK nodes **MUST** have `next_node_id` in config
- ❌ QUESTION nodes without conditions **MUST** have `next_node_id` in config
- ❌ QUESTION nodes with conditions **MUST** have `next_node_id` in each option

**This is the gap that allows users to create flows with the loop bug.**

---

## Solution 1: Enhanced Backend Validation (REQUIRED)

### Add Validation to `flow_service.py`

Update `_validate_nodes()` method to enforce `next_node_id` requirement:

```python
# app/services/flow_service.py (after line 348)

def _validate_nodes(self, nodes: dict) -> None:
    """
    Validate flow node structure.

    Args:
        nodes: Flow nodes configuration

    Raises:
        ValueError: If node structure is invalid
    """
    # ... existing validation code ...

    # NEW: Validate next_node_id exists for non-terminal nodes
    TERMINAL_NODE_TYPES = {"end", "escalate"}
    BRANCHING_NODE_TYPES = {"question", "condition"}

    for node in node_list:
        node_id = node.get("id")
        node_type = node.get("type")
        config = node.get("config", {})

        # Skip terminal nodes (they don't need next_node_id)
        if node_type in TERMINAL_NODE_TYPES:
            continue

        # Check branching nodes (QUESTION, CONDITION)
        if node_type in BRANCHING_NODE_TYPES:
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
                        f"QUESTION node '{node_id}' must have either:\n"
                        f"  - 'next_node_id' in config (all options go to same node), OR\n"
                        f"  - 'next_node_id' in EVERY option (conditional branching)\n"
                        f"Without this, the flow will loop indefinitely on this node."
                    )

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

        # Check linear flow nodes (MESSAGE, TEXT_INPUT, RAG_QUERY, WEBHOOK, etc.)
        else:
            if "next_node_id" not in config:
                raise ValueError(
                    f"{node_type.upper()} node '{node_id}' must have 'next_node_id' in config.\n"
                    f"Without this, the flow will loop indefinitely on this node.\n"
                    f"Tip: Set 'next_node_id' to the ID of the next node in the flow."
                )
```

### Where to Add This Code

**File:** `app/services/flow_service.py`
**Location:** Inside the `_validate_nodes()` method, after line 348
**Method:** Add to the end of the existing validation, before the method returns

---

## Solution 2: Frontend Flow Builder Validation (RECOMMENDED)

### Flow Builder UI Validation

If you have a visual flow builder in the frontend, add client-side validation:

```typescript
// frontend/src/components/FlowBuilder/validation.ts

interface FlowNode {
  id: string;
  type: string;
  config: {
    next_node_id?: string;
    options?: Array<{next_node_id?: string}>;
    default_node_id?: string;
  };
}

const TERMINAL_NODE_TYPES = ['end', 'escalate'];
const BRANCHING_NODE_TYPES = ['question', 'condition'];

export function validateFlowNodes(nodes: FlowNode[]): {
  isValid: boolean;
  errors: string[];
} {
  const errors: string[] = [];

  for (const node of nodes) {
    // Skip terminal nodes
    if (TERMINAL_NODE_TYPES.includes(node.type)) {
      continue;
    }

    // Validate QUESTION nodes
    if (node.type === 'question') {
      const hasConfigNext = 'next_node_id' in node.config;
      const options = node.config.options || [];
      const allOptionsHaveNext = options.every(opt => 'next_node_id' in opt);

      if (!hasConfigNext && !allOptionsHaveNext) {
        errors.push(
          `Question node "${node.id}" needs next_node_id (either in config or in all options)`
        );
      }
    }

    // Validate linear flow nodes
    else if (!BRANCHING_NODE_TYPES.includes(node.type)) {
      if (!node.config.next_node_id) {
        errors.push(
          `${node.type.toUpperCase()} node "${node.id}" missing next_node_id`
        );
      }
    }
  }

  return {
    isValid: errors.length === 0,
    errors,
  };
}
```

### UI Warnings

Show validation errors in the flow builder:

```tsx
// When user tries to save/activate flow
const validationResult = validateFlowNodes(flow.nodes);

if (!validationResult.isValid) {
  showErrorToast({
    title: "Flow Configuration Error",
    description: "Some nodes are missing connections",
    errors: validationResult.errors,
    action: "Please connect all nodes before saving"
  });
  return;
}
```

---

## Solution 3: Database Migration for Existing Flows (OPTIONAL)

### Validate Existing Flows

Create a script to find and fix existing flows with missing `next_node_id`:

```python
# scripts/validate_all_flows.py

"""
Validate all flows in database for missing next_node_id.

Usage:
    poetry run python scripts/validate_all_flows.py --fix
"""

import argparse
from app.db.session import SessionLocal
from app.db.models.flow import Flow
from sqlalchemy import select

TERMINAL_TYPES = {"end", "escalate"}

def validate_flow(flow: Flow) -> list[str]:
    """Return list of validation errors."""
    errors = []
    nodes = flow.nodes.get("nodes", [])

    for node in nodes:
        node_id = node.get("id")
        node_type = node.get("type")
        config = node.get("config", {})

        if node_type in TERMINAL_TYPES:
            continue

        if node_type == "question":
            has_config_next = "next_node_id" in config
            options = config.get("options", [])
            all_options_have_next = all("next_node_id" in opt for opt in options)

            if not has_config_next and not all_options_have_next:
                errors.append(f"QUESTION node '{node_id}' missing next_node_id")

        elif node_type not in {"condition"}:
            if "next_node_id" not in config:
                errors.append(f"{node_type.upper()} node '{node_id}' missing next_node_id")

    return errors

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fix", action="store_true", help="Auto-fix flows using edges")
    args = parser.parse_args()

    db = SessionLocal()

    try:
        stmt = select(Flow).where(Flow.is_active == True)
        flows = db.execute(stmt).scalars().all()

        print(f"Checking {len(flows)} active flows...")

        invalid_flows = []
        for flow in flows:
            errors = validate_flow(flow)
            if errors:
                invalid_flows.append((flow, errors))

        if not invalid_flows:
            print("✅ All flows are valid!")
            return

        print(f"\n❌ Found {len(invalid_flows)} flows with issues:\n")

        for flow, errors in invalid_flows:
            print(f"Flow: {flow.name} (ID: {flow.id})")
            print(f"  Tenant: {flow.tenant_id}")
            for error in errors:
                print(f"  - {error}")
            print()

        if args.fix:
            print("Fixing flows...")
            # TODO: Implement auto-fix using edges array
            print("⚠️  Auto-fix not implemented yet")
            print("Please manually update flows or use fix_flow_loop.py")
        else:
            print("Run with --fix to attempt auto-repair")

    finally:
        db.close()

if __name__ == "__main__":
    main()
```

---

## Solution 4: Documentation & User Guidance

### Update Flow Builder Help Text

Add tooltips/help text in the dashboard:

**When creating a MESSAGE node:**
> ℹ️ **Next Step Required**
> MESSAGE nodes automatically advance to the next node after displaying the message.
> You must specify which node to go to next, or the flow will get stuck.

**When creating a QUESTION node:**
> ℹ️ **Button Actions Required**
> Each button option must have a next action.
> Choose which node to go to when each button is clicked.

**When user doesn't connect a node:**
> ⚠️ **Warning: Unconnected Node**
> This node doesn't have a next step. The flow will stop here.
> Connect this node to another node or add an END node.

### Add Validation Endpoint

Create an endpoint to validate flows before activation:

```python
# app/api/v1/endpoints/flows.py

@router.post("/{flow_id}/validate", response_model=FlowValidationResponse)
def validate_flow(
    flow_id: UUID,
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_tenant_admin),
):
    """
    Validate flow configuration.

    Checks for common issues like missing next_node_id, broken references, etc.
    """
    flow = db.get(Flow, flow_id)
    if not flow:
        raise HTTPException(status_code=404, detail="Flow not found")

    verify_tenant_access(flow.tenant_id, current_user)

    errors = []
    warnings = []

    try:
        # Use existing validation
        flow_service = FlowService(db)
        flow_service._validate_nodes(flow.nodes)

        # Additional checks
        nodes = flow.nodes.get("nodes", [])
        if len(nodes) < 2:
            warnings.append("Flow has less than 2 nodes. Consider adding more steps.")

        # Check for orphaned nodes (nodes that can't be reached)
        # ... (implementation omitted for brevity)

        return FlowValidationResponse(
            is_valid=True,
            errors=[],
            warnings=warnings
        )

    except ValueError as e:
        return FlowValidationResponse(
            is_valid=False,
            errors=[str(e)],
            warnings=warnings
        )
```

---

## Implementation Checklist

### Backend (REQUIRED)
- [ ] Update `app/services/flow_service.py:_validate_nodes()` with enhanced validation
- [ ] Add unit tests for new validation logic
- [ ] Run validation on all existing flows in database
- [ ] Fix any flows that fail validation

### Frontend (RECOMMENDED)
- [ ] Add client-side validation in flow builder
- [ ] Show clear error messages for missing connections
- [ ] Add visual indicators for unconnected nodes
- [ ] Prevent saving flows with validation errors

### Database (OPTIONAL)
- [ ] Run `scripts/validate_all_flows.py` to find broken flows
- [ ] Fix or deactivate broken flows
- [ ] Document any flows that couldn't be auto-fixed

### Documentation (REQUIRED)
- [ ] Update flow builder user guide with connection requirements
- [ ] Add troubleshooting section for loop bugs
- [ ] Create video tutorial on flow creation best practices

---

## Testing the Fix

### 1. Create Test Flow Without next_node_id

Try creating a flow via API without `next_node_id`:

```bash
curl -X POST http://localhost:8000/api/v1/flows \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{
    "tenant_id": "...",
    "name": "Test Flow",
    "description": "Testing validation",
    "trigger_type": "keyword",
    "trigger_config": {"keywords": ["test"]},
    "nodes": {
      "start_node_id": "msg1",
      "nodes": [
        {
          "id": "msg1",
          "type": "message",
          "config": {
            "message": "Hello!"
            // ❌ Missing next_node_id
          }
        },
        {
          "id": "end",
          "type": "end",
          "config": {}
        }
      ],
      "edges": []
    }
  }'
```

**Expected Response:**
```json
{
  "detail": "Failed to create flow: MESSAGE node 'msg1' must have 'next_node_id' in config.\nWithout this, the flow will loop indefinitely on this node.\nTip: Set 'next_node_id' to the ID of the next node in the flow."
}
```

### 2. Create Valid Flow With next_node_id

```bash
curl -X POST http://localhost:8000/api/v1/flows \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{
    "tenant_id": "...",
    "name": "Test Flow",
    "description": "Testing validation",
    "trigger_type": "keyword",
    "trigger_config": {"keywords": ["test"]},
    "nodes": {
      "start_node_id": "msg1",
      "nodes": [
        {
          "id": "msg1",
          "type": "message",
          "config": {
            "message": "Hello!",
            "next_node_id": "end"  // ✅ Has next_node_id
          }
        },
        {
          "id": "end",
          "type": "end",
          "config": {}
        }
      ],
      "edges": []
    }
  }'
```

**Expected Response:** HTTP 201 Created ✅

---

## Summary

### Root Cause
Users creating flows via dashboard could create the same loop bug as the templates because validation only checked IF `next_node_id` exists, not that it MUST exist.

### Solution
1. **Backend Validation (REQUIRED):** Enhance `_validate_nodes()` to require `next_node_id` for non-terminal nodes
2. **Frontend Validation (RECOMMENDED):** Add client-side checks in flow builder UI
3. **Database Cleanup (OPTIONAL):** Validate and fix existing flows
4. **User Guidance (REQUIRED):** Update documentation and add helpful UI messages

### Priority
- **P0 (Must Do):** Backend validation enhancement
- **P1 (Should Do):** Frontend validation + user guidance
- **P2 (Nice to Have):** Database cleanup script

---

**Once implemented, users will be prevented from creating flows with the loop bug! 🎉**
