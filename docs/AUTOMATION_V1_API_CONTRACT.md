# Automation V1 API Contract

## Scope

Automation V1 introduces tenant-defined actions and audited action execution.

It extends existing flow runtime with a new node type:
- `action_call`

Legacy `webhook` node remains supported.

## Data Model (Implemented)

### `tenant_actions`
- Tenant-scoped action definitions.
- Supports HTTP method, URL template, auth config, request templates, schemas, and policy flags.

### `action_executions`
- Immutable audit trail of each action invocation.
- Stores request metadata, response details, status, latency, and errors.

## Endpoints (Implemented)

Base prefix: `/api/v1/tenant/automation`

### Actions

1. `POST /actions`
- Create action.
- Auth: tenant admin.

2. `GET /actions`
- List tenant actions.
- Auth: tenant admin.

3. `GET /actions/{action_id}`
- Get action detail.
- Auth: tenant admin.

4. `PATCH /actions/{action_id}`
- Update action.
- Auth: tenant admin.

5. `DELETE /actions/{action_id}`
- Delete action.
- Auth: tenant admin.

6. `POST /actions/{action_id}/test`
- Execute action in manual test mode with input payload.
- Auth: tenant admin.

### Executions

1. `GET /executions`
- List action executions.
- Optional filters: `action_id`, `limit`.
- Auth: tenant admin.

2. `GET /executions/{execution_id}`
- Get execution detail.
- Auth: tenant admin.

3. `POST /executions/{execution_id}/retry`
- Retry a previous execution with the same input payload.
- Auth: tenant admin.

4. `POST /executions/{execution_id}/approval`
- Approve/reject an approval-gated execution.
- Auth: tenant admin.

### Secrets

1. `POST /secrets`
- Create or update a tenant secret by `(provider, key_name)`.

2. `GET /secrets`
- List tenant secrets (masked only).

3. `DELETE /secrets/{secret_id}`
- Delete tenant secret.

## Runtime Node Contract (Implemented)

`action_call` node config:

```json
{
  "action_id": "UUID",
  "input_map": {
    "key": "{{variable}}"
  },
  "output_save_to": "action_result",
  "output_map": {
    "target_var": "$.normalized.data.field"
  },
  "next_node_id": "node_default_next",
  "on_success_next_node_id": "node_success",
  "on_failure_next_node_id": "node_failure",
  "on_timeout_next_node_id": "node_timeout",
  "on_policy_block_next_node_id": "node_policy",
  "on_approval_required_next_node_id": "node_approval"
}
```

Routing behavior:
- success -> `on_success_next_node_id` fallback `next_node_id`
- timeout -> `on_timeout_next_node_id` fallback `on_failure_next_node_id`
- policy block/approval required -> `on_policy_block_next_node_id` fallback `on_approval_required_next_node_id` fallback `on_failure_next_node_id`
- failure -> `on_failure_next_node_id`

If no route exists, node returns flow error and escalates.

## Security/Policy (Implemented)

- Outbound URL safety checks (`http/https` only, no localhost/private/internal ranges).
- Disabled/inactive actions are blocked.
- `allow_in_flows=false` blocks flow runtime usage.
- `requires_approval=true` is blocked in initial runtime call and can be approved via API.
- Flow save-time validation checks that each `action_call` references a real tenant action.

## Not Yet in V1 Runtime

These are planned next:
- replace plaintext `secret_value` storage with encrypted/vault-backed storage
- stronger schema validation and JSONPath engine
- dashboard UI for automation action/secret/execution management
- auto-resume flow execution after approval for in-flight conversations
