# Luran Support Flows - Implementation Guide

This document guides you through implementing the 5 nested conversational flows for Luran's own customer support.

## Overview

**Total Flows:** 5
**Total Nodes:** ~190-215 nodes
**Implementation Time:** ~10-15 minutes

### The 5 Flows

1. **🚀 Getting Started (Onboarding)** - ~45-50 nodes
   - Guides new users through setup: account verification → knowledge upload → widget integration → testing

2. **📁 Knowledge Upload Issues** - ~35-40 nodes
   - Troubleshoots file uploads, processing failures, format issues, storage limits

3. **🔧 Widget Integration Issues** - ~40-45 nodes
   - Solves widget setup, CORS errors, configuration problems

4. **💳 Account & Subscription** - ~40-45 nodes
   - Handles trial info, plan upgrades, billing, cancellations, account access

5. **🔍 Troubleshooting & Errors** - ~30-35 nodes
   - General error handling and routing to specialized flows

---

## Prerequisites

### 1. Tenant ID

You need Luran's own tenant ID to create these flows. This should be the tenant representing Luran's internal support system.

**Find your tenant ID:**
```bash
# Option 1: Query database
psql -d luran_db -c "SELECT id, name FROM tenants WHERE name LIKE '%Luran%' OR name LIKE '%Support%';"

# Option 2: Check via API
curl -X GET http://localhost:8000/api/v1/tenants \
  -H "Authorization: Bearer $ADMIN_TOKEN" | jq '.[] | {id, name}'
```

**If you don't have a Luran tenant yet:**
```bash
# Create one via API or database
# This should be the tenant that represents Luran's own support chatbot
```

### 2. Knowledge Base

For the RAG_QUERY nodes to work effectively, you should have uploaded knowledge about:
- Subscription plans (Trial, Lite, Professional, Growth, Enterprise)
- Pricing and features by plan
- Storage limits per plan
- File size limits
- CORS configuration steps
- Trial duration and limitations
- Widget integration guides
- API documentation

Upload these documents to your Luran support tenant's knowledge base before running the flows.

### 3. Database Access

Ensure your database is running and accessible:
```bash
# Test connection
python -c "from app.db.session import SessionLocal; db = SessionLocal(); print('✓ Database connected'); db.close()"
```

---

## Installation Steps

### Step 1: Set Environment Variable

```bash
# Set your Luran tenant ID
export LURAN_TENANT_ID="your-tenant-id-here"

# Verify it's set
echo $LURAN_TENANT_ID
```

Alternatively, you can pass it via command-line flag later.

### Step 2: Run Part 1 (Flows 1-3)

This creates:
- Flow 1: Getting Started
- Flow 2: Knowledge Upload Issues
- Flow 3: Widget Integration Issues

```bash
cd /Users/adebayovictor/Documents/dev/luran/luran

python scripts/create_luran_support_flows.py --tenant-id $LURAN_TENANT_ID
```

**Expected output:**
```
Creating Luran support flows for tenant: abc-123-def-456

✓ Found tenant: Luran Support

Creating Flow 1: Getting Started...
✅ Created Flow 1: Getting Started with 48 nodes

Creating Flow 2: Knowledge Upload Issues...
✅ Created Flow 2: Knowledge Upload Issues with 38 nodes

Creating Flow 3: Widget Integration Issues...
✅ Created Flow 3: Widget Integration Issues with 42 nodes

============================================================
✅ Successfully created 3 flows!
============================================================

Flow 1: Getting Started (ID: flow_id_1)
Flow 2: Knowledge Upload Issues (ID: flow_id_2)
Flow 3: Widget Integration Issues (ID: flow_id_3)

Next steps:
1. Run the second script to create flows 4 & 5
2. Test each flow via chat
3. Monitor analytics for completion rates
```

### Step 3: Run Part 2 (Flows 4-5)

This creates:
- Flow 4: Account & Subscription
- Flow 5: Troubleshooting & Errors

```bash
python scripts/create_luran_support_flows_part2.py --tenant-id $LURAN_TENANT_ID
```

**Expected output:**
```
Creating Luran support flows (Part 2) for tenant: abc-123-def-456

✓ Found tenant: Luran Support

Creating Flow 4: Account & Subscription...
✅ Created Flow 4: Account & Subscription with 72 nodes

Creating Flow 5: Troubleshooting & Errors...
✅ Created Flow 5: Troubleshooting & Errors with 85 nodes

============================================================
✅ Successfully created flows 4 & 5!
============================================================

Flow 4: Account & Subscription (ID: flow_id_4)
Flow 5: Troubleshooting & Errors (ID: flow_id_5)

🎉 All 5 Luran support flows are now complete!

Next steps:
1. Test each flow via chat
2. Monitor analytics for completion rates
3. Refine based on user feedback
```

---

## Verification

### Check Flows Were Created

```bash
# Via API
curl -X GET http://localhost:8000/api/v1/flows \
  -H "X-Tenant-ID: $LURAN_TENANT_ID" \
  -H "X-API-Key: $API_KEY" | jq '.[] | {name, trigger_keywords, node_count}'

# Via database
psql -d luran_db -c "
SELECT
  f.id,
  f.name,
  COUNT(fn.id) as node_count,
  f.trigger_keywords
FROM flows f
LEFT JOIN flow_nodes fn ON f.id = fn.flow_id
WHERE f.tenant_id = '$LURAN_TENANT_ID'
GROUP BY f.id, f.name, f.trigger_keywords
ORDER BY f.created_at;
"
```

**Expected output:**
```
┌──────────────┬──────────────────────────────┬────────────┬────────────────────────────┐
│ id           │ name                         │ node_count │ trigger_keywords           │
├──────────────┼──────────────────────────────┼────────────┼────────────────────────────┤
│ flow_id_1    │ Getting Started              │ 48         │ {getting started, setup}   │
│ flow_id_2    │ Knowledge Upload Issues      │ 38         │ {upload problem}           │
│ flow_id_3    │ Widget Integration Issues    │ 42         │ {widget not working, CORS} │
│ flow_id_4    │ Account & Subscription       │ 72         │ {upgrade, billing, trial}  │
│ flow_id_5    │ Troubleshooting & Errors     │ 85         │ {error, not working, help} │
└──────────────┴──────────────────────────────┴────────────┴────────────────────────────┘
```

### Verify Node Configurations

Check that nodes have proper structure:

```bash
psql -d luran_db -c "
SELECT
  fn.node_type,
  COUNT(*) as count
FROM flow_nodes fn
JOIN flows f ON fn.flow_id = f.id
WHERE f.tenant_id = '$LURAN_TENANT_ID'
GROUP BY fn.node_type
ORDER BY count DESC;
"
```

**Expected distribution:**
- MESSAGE: ~70-80 nodes
- QUESTION: ~40-50 nodes
- RAG_QUERY: ~20-25 nodes
- ESCALATE: ~25-30 nodes
- TEXT_INPUT: ~15-20 nodes
- END: ~15-20 nodes
- START: 5 nodes

---

## Testing

### Test Flow 1: Getting Started

```bash
# Send trigger message
curl -X POST http://localhost:8000/api/v1/chat/message \
  -H "Content-Type: application/json" \
  -H "X-Tenant-ID: $LURAN_TENANT_ID" \
  -H "X-API-Key: $API_KEY" \
  -d '{
    "message": "getting started",
    "user_identifier": "test@example.com"
  }' | jq '.message, .buttons'
```

**Expected response:**
```json
{
  "message": "👋 Welcome to Luran! I'll help you get set up with your AI-powered customer support. Let's get started!",
  "buttons": [
    {"text": "Just signed up", "value": "Just signed up"},
    {"text": "Uploaded knowledge", "value": "Uploaded knowledge"},
    {"text": "Integrated widget", "value": "Integrated widget"},
    {"text": "Already live", "value": "Already live"}
  ]
}
```

### Test Flow 2: Knowledge Upload Issues

```bash
curl -X POST http://localhost:8000/api/v1/chat/message \
  -H "Content-Type: application/json" \
  -H "X-Tenant-ID: $LURAN_TENANT_ID" \
  -H "X-API-Key: $API_KEY" \
  -d '{
    "message": "upload problem",
    "user_identifier": "test@example.com"
  }' | jq '.message'
```

**Expected:**
Flow starts with diagnostic message about upload troubleshooting.

### Test Flow 3: Widget Integration Issues

```bash
curl -X POST http://localhost:8000/api/v1/chat/message \
  -H "Content-Type: application/json" \
  -H "X-Tenant-ID: $LURAN_TENANT_ID" \
  -H "X-API-Key: $API_KEY" \
  -d '{
    "message": "CORS error",
    "user_identifier": "test@example.com"
  }' | jq '.message'
```

**Expected:**
Flow starts with CORS troubleshooting guidance.

### Test Flow 4: Account & Subscription

```bash
curl -X POST http://localhost:8000/api/v1/chat/message \
  -H "Content-Type: application/json" \
  -H "X-Tenant-ID: $LURAN_TENANT_ID" \
  -H "X-API-Key: $API_KEY" \
  -d '{
    "message": "upgrade",
    "user_identifier": "test@example.com"
  }' | jq '.message'
```

**Expected:**
Flow starts with upgrade/plan information.

### Test Flow 5: Troubleshooting & Errors

```bash
curl -X POST http://localhost:8000/api/v1/chat/message \
  -H "Content-Type: application/json" \
  -H "X-Tenant-ID: $LURAN_TENANT_ID" \
  -H "X-API-Key: $API_KEY" \
  -d '{
    "message": "error",
    "user_identifier": "test@example.com"
  }' | jq '.message'
```

**Expected:**
Flow starts with general diagnostic questions.

### End-to-End Testing

Test a complete flow path:

```bash
# 1. Start flow
SESSION_ID=$(curl -X POST http://localhost:8000/api/v1/chat/message \
  -H "Content-Type: application/json" \
  -H "X-Tenant-ID: $LURAN_TENANT_ID" \
  -H "X-API-Key: $API_KEY" \
  -d '{"message": "getting started", "user_identifier": "test@example.com"}' \
  | jq -r '.session_id')

echo "Session ID: $SESSION_ID"

# 2. Select first option
curl -X POST http://localhost:8000/api/v1/chat/message \
  -H "Content-Type: application/json" \
  -H "X-Tenant-ID: $LURAN_TENANT_ID" \
  -H "X-API-Key: $API_KEY" \
  -d "{\"session_id\": \"$SESSION_ID\", \"message\": \"Just signed up\"}" \
  | jq '.message, .buttons'

# 3. Continue with more selections...
# Follow the flow path to test full conversation
```

---

## Monitoring & Analytics

### Check Flow Usage

```sql
-- Flow completion rates
SELECT
  f.name,
  COUNT(DISTINCT fs.session_id) as total_sessions,
  COUNT(DISTINCT CASE WHEN fs.status = 'completed' THEN fs.session_id END) as completed,
  ROUND(100.0 * COUNT(DISTINCT CASE WHEN fs.status = 'completed' THEN fs.session_id END) /
        NULLIF(COUNT(DISTINCT fs.session_id), 0), 2) as completion_rate_percent
FROM flows f
LEFT JOIN flow_sessions fs ON f.id = fs.flow_id
WHERE f.tenant_id = '$LURAN_TENANT_ID'
GROUP BY f.id, f.name
ORDER BY total_sessions DESC;
```

### Find Escalation Points

```sql
-- Most common escalation subjects
SELECT
  fn.config->>'subject' as escalation_subject,
  COUNT(*) as escalation_count
FROM flow_sessions fs
JOIN flow_session_nodes fsn ON fs.id = fsn.flow_session_id
JOIN flow_nodes fn ON fsn.node_id = fn.id
JOIN flows f ON fn.flow_id = f.id
WHERE fn.node_type = 'ESCALATE'
  AND f.tenant_id = '$LURAN_TENANT_ID'
GROUP BY fn.config->>'subject'
ORDER BY escalation_count DESC
LIMIT 10;
```

### Identify Drop-off Points

```sql
-- Where users abandon flows
SELECT
  f.name as flow_name,
  fn.node_type,
  LEFT(fn.config->>'message', 50) as node_message,
  COUNT(*) as drop_offs
FROM flow_sessions fs
JOIN flow_session_nodes fsn ON fs.id = fsn.flow_session_id
JOIN flow_nodes fn ON fsn.node_id = fn.id
JOIN flows f ON fn.flow_id = f.id
WHERE fs.status = 'abandoned'
  AND f.tenant_id = '$LURAN_TENANT_ID'
GROUP BY f.name, fn.node_type, fn.config->>'message'
ORDER BY drop_offs DESC
LIMIT 15;
```

---

## Troubleshooting

### Issue: Flows not triggering

**Check trigger keywords:**
```sql
SELECT name, trigger_keywords
FROM flows
WHERE tenant_id = '$LURAN_TENANT_ID'
  AND is_active = true;
```

**Solution:** Ensure trigger keywords are properly formatted and match user messages.

### Issue: RAG_QUERY nodes failing

**Check if knowledge base has content:**
```sql
SELECT COUNT(*)
FROM knowledge_files
WHERE tenant_id = '$LURAN_TENANT_ID'
  AND status = 'processed';
```

**Solution:** Upload relevant knowledge documents covering:
- Subscription plans
- Pricing
- Features
- Troubleshooting guides

### Issue: Nodes not linking properly

**Check for orphaned nodes:**
```sql
SELECT
  fn.id,
  fn.node_type,
  fn.config->>'next_node_id' as next_node,
  fn2.id as next_exists
FROM flow_nodes fn
LEFT JOIN flow_nodes fn2 ON fn.config->>'next_node_id' = fn2.id::text
JOIN flows f ON fn.flow_id = f.id
WHERE f.tenant_id = '$LURAN_TENANT_ID'
  AND fn.config->>'next_node_id' IS NOT NULL
  AND fn2.id IS NULL;
```

**Solution:** If orphaned nodes found, fix the `next_node_id` references in the config.

### Issue: Database connection errors

**Check connection:**
```bash
python -c "from app.db.session import SessionLocal; SessionLocal().execute('SELECT 1').scalar()"
```

**Solution:** Verify DATABASE_URL in your .env file and that PostgreSQL is running.

---

## Customization

### Modify Trigger Keywords

```python
# Update trigger keywords for a flow
from app.db.session import SessionLocal
from app.db.models.flow import Flow

db = SessionLocal()
flow = db.query(Flow).filter(Flow.name == "Getting Started").first()
flow.trigger_keywords.extend(["onboard", "begin", "start luran"])
db.commit()
```

### Add New Nodes

```python
from app.db.models.flow import FlowNode, NodeType

# Create a new node
new_node = FlowNode(
    flow_id=flow.id,
    node_type=NodeType.MESSAGE,
    config={
        "message": "This is a new message!",
        "next_node_id": "existing_node_id"
    },
    position_x=1000,
    position_y=500
)
db.add(new_node)
db.commit()
```

### Update Messages

```python
# Update a node's message
node = db.query(FlowNode).filter(FlowNode.id == "node_id").first()
node.config["message"] = "Updated message text"
db.commit()
```

---

## Next Steps

### 1. Integrate with Chat Widget

Add these flows to your Luran support widget configuration so customers can trigger them naturally.

### 2. Train Support Team

Educate your support team on:
- How flows work
- When escalations occur
- How to handle escalated tickets

### 3. Monitor & Iterate

- Review analytics weekly
- Refine trigger keywords based on usage
- Add new branches for emerging issues
- A/B test message wording

### 4. Expand Knowledge Base

Continuously update knowledge base with:
- New feature documentation
- Updated pricing
- Common troubleshooting solutions
- Customer feedback insights

### 5. Create Flow Templates

Package these flows as templates that Luran customers can clone and customize for their own support.

---

## Support

If you encounter issues during implementation:

1. Check the error logs: `tail -f logs/luran.log`
2. Review database constraints and foreign keys
3. Verify tenant_id exists and is correct
4. Ensure knowledge base has content for RAG queries

For questions or issues, contact the development team or file an issue in the repository.

---

## Success Metrics

Track these KPIs after deployment:

- **Flow Completion Rate**: Target 70%+
- **Escalation Rate**: Target <30%
- **Average Resolution Time**: Track improvement over time
- **User Satisfaction**: Survey users after flow interactions
- **Deflection Rate**: Measure % of issues resolved without human intervention

---

## Appendix: Flow Structure Summary

### Flow 1: Getting Started (48 nodes)
```
START → Welcome → Setup Stage Q → [4 branches]
  ├─ Just signed up (20 nodes)
  ├─ Uploaded knowledge (12 nodes)
  ├─ Integrated widget (10 nodes)
  └─ Already live (6 nodes)
```

### Flow 2: Knowledge Upload Issues (38 nodes)
```
START → Diagnostic → Problem Type Q → [4 branches]
  ├─ Upload fails (12 nodes)
  ├─ Processing fails (10 nodes)
  ├─ Stuck processing (8 nodes)
  └─ Missing files (8 nodes)
```

### Flow 3: Widget Integration Issues (42 nodes)
```
START → Troubleshooting → Issue Type Q → [4 branches]
  ├─ Widget doesn't appear (15 nodes)
  ├─ CORS error (10 nodes)
  ├─ Widget broken (12 nodes)
  └─ Configuration (5 nodes)
```

### Flow 4: Account & Subscription (72 nodes)
```
START → Support → Help Type Q → [5 branches]
  ├─ Trial information (12 nodes)
  ├─ Upgrade plan (20 nodes)
  ├─ Billing issue (25 nodes)
  ├─ Plan comparison (10 nodes)
  └─ Account access (5 nodes)
```

### Flow 5: Troubleshooting & Errors (85 nodes)
```
START → Diagnostic → Problem Area Q → [6 branches]
  ├─ Chat/AI responses (30 nodes)
  ├─ Knowledge base → redirect (2 nodes)
  ├─ Widget → redirect (2 nodes)
  ├─ Dashboard (20 nodes)
  ├─ API (25 nodes)
  └─ Not sure (6 nodes)
```

**Total: 285 nodes across 5 flows**

---

*Last updated: 2026-02-06*
