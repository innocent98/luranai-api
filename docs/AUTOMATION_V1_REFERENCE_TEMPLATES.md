# Automation V1 Reference Templates

This document provides concrete, business-ready starter templates for Luran Automation V1.

These are examples only. The automation model supports any business domain that can expose HTTP APIs.

## 1) E-commerce: Order Tracking

### Action: `track_order`
```json
{
  "name": "Track Order",
  "slug": "track_order",
  "description": "Get current delivery status for an order",
  "status": "active",
  "type": "http",
  "method": "GET",
  "url_template": "https://api.store.com/v1/orders/{{order_id}}",
  "timeout_seconds": 20,
  "auth_type": "bearer",
  "auth_config": {
    "bearer_token": "REPLACE_WITH_SECRET_REF_OR_RUNTIME_SECRET"
  },
  "headers_template": {
    "X-Customer-Email": "{{customer_email}}"
  },
  "query_template": {},
  "body_template": {},
  "input_schema": {
    "type": "object",
    "required": ["order_id", "customer_email"],
    "properties": {
      "order_id": {"type": "string"},
      "customer_email": {"type": "string"}
    }
  },
  "output_schema": {
    "type": "object",
    "properties": {
      "status": {"type": "string"},
      "eta": {"type": "string"},
      "tracking_url": {"type": "string"}
    }
  },
  "allow_in_flows": true
}
```

### Flow node: `action_call`
```json
{
  "id": "track_order_node",
  "type": "action_call",
  "config": {
    "action_id": "UUID_OF_track_order",
    "input_map": {
      "order_id": "{{order_id}}",
      "customer_email": "{{customer_email}}"
    },
    "output_save_to": "order_tracking",
    "output_map": {
      "order_status": "$.normalized.data.status",
      "order_eta": "$.normalized.data.eta",
      "tracking_url": "$.normalized.data.tracking_url"
    },
    "on_success_next_node_id": "show_order_status",
    "on_failure_next_node_id": "order_tracking_failed",
    "on_timeout_next_node_id": "order_tracking_timeout"
  }
}
```

## 2) Appointment Booking

### Action: `check_slot_availability`
```json
{
  "name": "Check Slot Availability",
  "slug": "check_slot_availability",
  "method": "GET",
  "url_template": "https://api.clinic.com/v1/slots",
  "auth_type": "api_key_header",
  "auth_config": {
    "header_name": "X-API-Key",
    "api_key": "REPLACE_WITH_SECRET_REF_OR_RUNTIME_SECRET"
  },
  "query_template": {
    "date": "{{appointment_date}}",
    "service": "{{service_type}}"
  },
  "input_schema": {
    "type": "object",
    "required": ["appointment_date", "service_type"]
  }
}
```

### Action: `create_appointment`
```json
{
  "name": "Create Appointment",
  "slug": "create_appointment",
  "method": "POST",
  "url_template": "https://api.clinic.com/v1/appointments",
  "auth_type": "api_key_header",
  "auth_config": {
    "header_name": "X-API-Key",
    "api_key": "REPLACE_WITH_SECRET_REF_OR_RUNTIME_SECRET"
  },
  "body_template": {
    "date": "{{appointment_date}}",
    "time": "{{selected_time}}",
    "service": "{{service_type}}",
    "email": "{{customer_email}}",
    "name": "{{customer_name}}"
  },
  "allow_in_flows": true
}
```

### Flow pattern
1. Collect date/service
2. `action_call` -> `check_slot_availability`
3. Ask user to choose time
4. `action_call` -> `create_appointment`
5. Confirm booking ID and next steps

## 3) Account Recovery: Unlock Suspended Account

### Action: `check_account_status`
```json
{
  "name": "Check Account Status",
  "slug": "check_account_status",
  "method": "GET",
  "url_template": "https://api.product.com/v1/users/status",
  "auth_type": "bearer",
  "auth_config": {"bearer_token": "REPLACE_WITH_SECRET_REF_OR_RUNTIME_SECRET"},
  "query_template": {"email": "{{customer_email}}"},
  "allow_in_flows": true
}
```

### Action: `unlock_account`
```json
{
  "name": "Unlock Account",
  "slug": "unlock_account",
  "method": "POST",
  "url_template": "https://api.product.com/v1/users/unlock",
  "auth_type": "bearer",
  "auth_config": {"bearer_token": "REPLACE_WITH_SECRET_REF_OR_RUNTIME_SECRET"},
  "body_template": {
    "email": "{{customer_email}}",
    "reason": "support_verified_unlock"
  },
  "is_sensitive": true,
  "requires_approval": true,
  "allow_in_flows": true
}
```

### Recommended flow behavior
1. Collect email + verification signal
2. `action_call` -> `check_account_status`
3. If suspended and policy allows:
   - escalate to approval path (human-in-the-loop) OR run unlock after approved
4. `action_call` -> `unlock_account`
5. Confirm unlock and send follow-up instructions

## Notes

- Appointment booking is fully supported in this model.
- These templates are intentionally provider-agnostic.
- Replace placeholder secrets with secure secret references before production use.
