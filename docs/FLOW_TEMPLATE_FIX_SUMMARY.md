# Flow Template Loop Bug - Fix Summary

**Date:** 2026-02-06
**Status:** ✅ FAQ Handler Fixed | ⏳ 9 Templates Remaining

---

## Root Cause

The flow templates in `scripts/seed_flow_templates.py` use an `edges` array to define node connections, but the `FlowEngine` execution logic expects `next_node_id` directly in each node's config.

### What Happens Without the Fix:

1. User sends message matching flow trigger (e.g., "help", "faq")
2. Flow engine matches FAQ Handler flow
3. Executes first node ("greet" MESSAGE node)
4. MESSAGE nodes auto-advance by calling `_get_next_node()`
5. `_get_next_node()` looks for `config.get("next_node_id")`
6. Returns `None` because it's not in the config
7. Flow doesn't advance → **Loops on same message** ❌

### How the Flow Engine Works:

```python
# app/services/flow_engine.py:689-701
def _get_next_node(self, current_node: dict, execution: FlowExecution) -> Optional[str]:
    config = current_node.get("config", {})
    return config.get("next_node_id")  # ⬅️ Expects next_node_id here!
```

---

## What Was Fixed: FAQ Handler Template

### Before (Buggy):

```python
{
    "id": "greet",
    "type": "message",
    "config": {
        "message": "I can help answer your questions! What would you like to know?"
        # ❌ Missing next_node_id!
    }
}
```

### After (Fixed):

```python
{
    "id": "greet",
    "type": "message",
    "config": {
        "message": "I can help answer your questions! What would you like to know?",
        "next_node_id": "get_question"  # ✅ Added!
    }
}
```

### All FAQ Handler Fixes:

1. ✅ `greet` MESSAGE → added `"next_node_id": "get_question"`
2. ✅ `get_question` TEXT_INPUT → added `"next_node_id": "search_knowledge"`
3. ✅ `search_knowledge` RAG_QUERY → added `"next_node_id": "provide_answer"`
4. ✅ `provide_answer` MESSAGE → added `"next_node_id": "satisfaction_check"`
5. ✅ `satisfaction_check` QUESTION → added `next_node_id` to each option:
   - `"yes"` option → `"next_node_id": "end"`
   - `"no"` option → `"next_node_id": "escalate"`

---

## Remaining Templates to Fix

### Sales Category (4 templates):

1. **Welcome & Lead Qualification** (Lines ~27-116)
   - MESSAGE nodes: `welcome`, `thank_you`
   - TEXT_INPUT nodes: `ask_name`, `ask_company`, `ask_need`
   - WEBHOOK node: `webhook_crm`

2. **Product Demo Request** (Lines ~204-290)
   - MESSAGE node: `intro`, `confirm`
   - TEXT_INPUT nodes: `get_name`, `get_email`
   - QUESTION node: `get_company_size` (add next_node_id to options)
   - WEBHOOK node: `webhook_demo`

3. **Appointment Booking** (Lines ~534-623)
   - MESSAGE node: `intro`, `confirm`
   - TEXT_INPUT nodes: `get_name`, `get_email`, `get_phone`
   - QUESTION node: `get_time_preference` (add next_node_id to options)
   - WEBHOOK node: `webhook_calendar`

4. **Upgrade Inquiry** (Lines ~1126-1213)
   - MESSAGE nodes: `intro`, `explain_benefits`, `pricing_info`
   - QUESTION node: `ask_plan_interest` (add next_node_id to options)
   - TEXT_INPUT node: `get_contact`
   - WEBHOOK node: `webhook_sales`

### Support Category (4 templates):

5. **Billing Inquiry** (Lines ~292-396)
   - MESSAGE node: `greet`
   - QUESTION node: `billing_issue_type` (add next_node_id to options)
   - RAG_QUERY nodes: `search_billing_help`
   - TEXT_INPUT nodes: various (check edges)
   - ESCALATE nodes: various (no next_node_id needed)

6. **Bug Report** (Lines ~398-532)
   - MESSAGE nodes: `intro`, `thank_you`
   - TEXT_INPUT nodes: `get_description`, `get_steps`, `get_expected`
   - QUESTION nodes: `get_severity`, `get_browser` (add next_node_id to options)
   - WEBHOOK node: `webhook_create_ticket`

7. **Refund Request** (Lines ~909-1010)
   - MESSAGE node: `intro`
   - QUESTION nodes: `refund_reason`, `order_satisfaction` (add next_node_id to options)
   - TEXT_INPUT nodes: `get_order_id`, `get_details`
   - RAG_QUERY node: `search_refund_policy`
   - WEBHOOK node: `webhook_refund`

### Engagement Category (2 templates):

8. **Feature Request** (Lines ~625-738)
   - MESSAGE nodes: `intro`, `thank_you`
   - TEXT_INPUT nodes: `get_feature`, `get_use_case`, `get_priority`
   - QUESTION node: `get_plan_tier` (add next_node_id to options)
   - WEBHOOK node: `webhook_feature_request`

9. **Customer Feedback** (Lines ~740-907)
   - MESSAGE node: `intro`
   - QUESTION nodes: `satisfaction_rating`, `recommend_rating`, `feedback_area` (add next_node_id to options)
   - TEXT_INPUT nodes: `get_comments`, `get_contact`
   - WEBHOOK node: `webhook_feedback`

---

## How to Fix Each Template

### For MESSAGE, TEXT_INPUT, RAG_QUERY, WEBHOOK nodes:

1. Find the node in the template
2. Look at the `edges` array to find `{"from": "node_id", "to": "next_id"}`
3. Add `"next_node_id": "next_id"` to the node's config
4. Keep the edges array (for backward compatibility)

### For QUESTION nodes:

1. Find the node in the template
2. Look at the `edges` array for conditional edges with this node as "from"
3. Match each edge's `condition.equals` value to an option's `value`
4. Add `"next_node_id": "edge_to_id"` to that option
5. Keep the conditional edges array

### Terminal nodes (END, ESCALATE):

- ✅ No changes needed - these don't advance the flow

---

## Testing the Fix

### 1. Re-seed the database:

```bash
poetry run python scripts/seed_flow_templates.py
```

### 2. Test FAQ Handler flow:

```bash
curl -X POST http://localhost:8000/api/v1/chat/message \
  -H "Content-Type: application/json" \
  -H "X-Tenant-ID: your-tenant-id" \
  -H "X-API-Key: your-api-key" \
  -d '{
    "message": "help",
    "user_identifier": "test@example.com"
  }'
```

### Expected behavior:

1. First response: "I can help answer your questions! What would you like to know?"
2. Second response: "Please type your question:" (TEXT_INPUT prompt)
3. User types question → Gets RAG answer
4. User clicks "Yes, thank you!" → Flow ends ✅

### What NOT to see:

- ❌ Same message repeating: "I can help answer your questions..."
- ❌ Flow stuck on first node
- ❌ No advancement after MESSAGE nodes

---

## Verification Checklist

After fixing each template:

- [ ] All MESSAGE nodes have `next_node_id` in config
- [ ] All TEXT_INPUT nodes have `next_node_id` in config
- [ ] All RAG_QUERY nodes have `next_node_id` in config
- [ ] All WEBHOOK nodes have `next_node_id` in config
- [ ] All QUESTION node options have `next_node_id` field
- [ ] END nodes have NO `next_node_id` (correct)
- [ ] ESCALATE nodes have NO `next_node_id` (correct)
- [ ] edges array still present (backward compatibility)

---

## Next Steps

1. ✅ FAQ Handler fixed and ready to test
2. ⏳ Fix remaining 9 templates (can be done incrementally)
3. 🧪 Test each fixed template before moving to next
4. 📊 Monitor flow completion rates after deployment

---

## Alternative: Automated Fix Script

If you want to fix all 10 templates automatically, you can use the script I created:

```bash
python3 scripts/fix_seed_file_simple.py
```

**Note:** This script uses regex to parse and update the seed file. It's been tested on the FAQ Handler template. Review changes before committing.

---

## Files Modified

- ✅ `scripts/seed_flow_templates.py` - FAQ Handler template fixed (lines 131-174)

## Files Created

- ✅ `scripts/fix_flow_templates.py` - Database-based fix script (requires DB connection)
- ✅ `scripts/fix_seed_file.py` - File-based fix script (exec approach)
- ✅ `scripts/fix_seed_file_simple.py` - File-based fix script (regex approach)
- ✅ `docs/FLOW_TEMPLATE_FIX_SUMMARY.md` - This document

---

**Ready to test the FAQ Handler fix!** 🚀

Once confirmed working, we can proceed with fixing the remaining 9 templates using the same pattern.
