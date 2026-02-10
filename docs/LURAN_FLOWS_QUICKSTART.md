# Luran Support Flows - Quick Start Guide

Get your Luran support flows up and running in 10 minutes.

---

## Prerequisites Checklist

Before you begin, ensure you have:

- [ ] PostgreSQL database running
- [ ] Luran application server configured
- [ ] Python environment activated
- [ ] Database migrations up to date
- [ ] A tenant ID for Luran's support system

---

## Step-by-Step Setup

### 1. Get Your Tenant ID (2 minutes)

**Option A: Query existing tenant**
```bash
# Find Luran support tenant
psql -d luran_db -c "SELECT id, name FROM tenants WHERE name ILIKE '%luran%';"
```

**Option B: Create new tenant**
```bash
# Via psql
psql -d luran_db -c "
INSERT INTO tenants (name, email, is_active)
VALUES ('Luran Support', 'support@luran.ai', true)
RETURNING id, name;
"

# Or via Python
python -c "
from app.db.session import SessionLocal
from app.db.models.tenant import Tenant

db = SessionLocal()
tenant = Tenant(name='Luran Support', email='support@luran.ai', is_active=True)
db.add(tenant)
db.commit()
print(f'Created tenant: {tenant.id}')
db.close()
"
```

**Save your tenant ID:**
```bash
export LURAN_TENANT_ID="your-tenant-id-here"
echo $LURAN_TENANT_ID
```

---

### 2. Upload Knowledge Base (3 minutes)

For RAG queries to work, upload documentation to your Luran support tenant:

**Required knowledge topics:**
- Subscription plans (Trial, Lite, Professional, Growth, Enterprise)
- Pricing and features comparison
- Storage and file size limits per plan
- CORS configuration guide
- Widget integration instructions
- API documentation
- Trial duration and limitations

**Upload via dashboard or API:**
```bash
# Example: Upload via API
curl -X POST http://localhost:8000/api/v1/knowledge/upload \
  -H "X-Tenant-ID: $LURAN_TENANT_ID" \
  -H "X-API-Key: $API_KEY" \
  -F "file=@docs/subscription_plans.pdf" \
  -F "file=@docs/widget_integration.pdf" \
  -F "file=@docs/api_documentation.pdf"
```

**Verify uploads:**
```bash
curl -X GET http://localhost:8000/api/v1/knowledge \
  -H "X-Tenant-ID: $LURAN_TENANT_ID" \
  -H "X-API-Key: $API_KEY" | jq '.[] | {filename, status}'
```

Wait for all files to show `"status": "processed"` before continuing.

---

### 3. Create Flows (5 minutes)

**Run Part 1 (Flows 1-3):**
```bash
cd /Users/adebayovictor/Documents/dev/luran/luran

python scripts/create_luran_support_flows.py --tenant-id $LURAN_TENANT_ID
```

Expected output:
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
```

**Run Part 2 (Flows 4-5):**
```bash
python scripts/create_luran_support_flows_part2.py --tenant-id $LURAN_TENANT_ID
```

Expected output:
```
Creating Flow 4: Account & Subscription...
✅ Created Flow 4: Account & Subscription with 72 nodes

Creating Flow 5: Troubleshooting & Errors...
✅ Created Flow 5: Troubleshooting & Errors with 85 nodes

🎉 All 5 Luran support flows are now complete!
```

---

### 4. Validate Flows (1 minute)

**Run validation script:**
```bash
python scripts/test_luran_flows.py --tenant-id $LURAN_TENANT_ID
```

Expected output:
```
============================================================
VALIDATING LURAN SUPPORT FLOWS
============================================================

1. Validating flow count...
   ✓ All 5 flows present

2. Validating trigger keywords...
   ✓ Getting Started: 9 keywords
   ✓ Knowledge Upload Issues: 8 keywords
   ...

✅ ALL VALIDATIONS PASSED!
```

If you see errors, fix them before proceeding to testing.

---

### 5. Test Flows (2 minutes)

**Test Flow 1: Getting Started**
```bash
curl -X POST http://localhost:8000/api/v1/chat/message \
  -H "Content-Type: application/json" \
  -H "X-Tenant-ID: $LURAN_TENANT_ID" \
  -H "X-API-Key: $API_KEY" \
  -d '{
    "message": "getting started",
    "user_identifier": "test@example.com"
  }' | jq '.'
```

**Expected response:**
- Flow should trigger
- Welcome message displayed
- Buttons shown for setup stages

**Test other flows:**
```bash
# Flow 2: Knowledge Upload
curl ... -d '{"message": "upload problem", ...}'

# Flow 3: Widget Integration
curl ... -d '{"message": "CORS error", ...}'

# Flow 4: Account & Subscription
curl ... -d '{"message": "upgrade", ...}'

# Flow 5: Troubleshooting
curl ... -d '{"message": "error", ...}'
```

---

## Verify Installation

**Check database:**
```sql
SELECT
  f.name,
  COUNT(fn.id) as node_count,
  array_length(f.trigger_keywords, 1) as keyword_count
FROM flows f
LEFT JOIN flow_nodes fn ON f.id = fn.flow_id
WHERE f.tenant_id = 'your-tenant-id'
GROUP BY f.id, f.name, f.trigger_keywords
ORDER BY f.created_at;
```

**Expected:**
```
┌──────────────────────────────┬────────────┬───────────────┐
│ name                         │ node_count │ keyword_count │
├──────────────────────────────┼────────────┼───────────────┤
│ Getting Started              │ 48         │ 9             │
│ Knowledge Upload Issues      │ 38         │ 8             │
│ Widget Integration Issues    │ 42         │ 8             │
│ Account & Subscription       │ 72         │ 9             │
│ Troubleshooting & Errors     │ 85         │ 9             │
└──────────────────────────────┴────────────┴───────────────┘

Total: ~285 nodes
```

---

## Common Issues & Solutions

### Issue: "Tenant not found"

**Solution:**
```bash
# Verify tenant ID is correct
psql -d luran_db -c "SELECT id, name FROM tenants WHERE id = '$LURAN_TENANT_ID';"

# If no results, create tenant first (see Step 1)
```

### Issue: "Database connection failed"

**Solution:**
```bash
# Check PostgreSQL is running
pg_isready

# Check connection string
python -c "from app.db.session import SessionLocal; db = SessionLocal(); print('Connected'); db.close()"

# Verify .env file has correct DATABASE_URL
cat .env | grep DATABASE_URL
```

### Issue: "RAG queries failing"

**Solution:**
```bash
# Check knowledge base has content
curl -X GET http://localhost:8000/api/v1/knowledge \
  -H "X-Tenant-ID: $LURAN_TENANT_ID" \
  -H "X-API-Key: $API_KEY" \
  | jq '. | length'

# Should return > 0

# If empty, upload knowledge documents (see Step 2)
```

### Issue: "Flows not triggering"

**Solution:**
```bash
# Check flows are active
psql -d luran_db -c "
SELECT name, is_active, trigger_keywords
FROM flows
WHERE tenant_id = '$LURAN_TENANT_ID';
"

# Ensure is_active = true
# Check trigger keywords match your test messages
```

### Issue: "Broken node references"

**Solution:**
```bash
# Run validation script to identify issues
python scripts/test_luran_flows.py --tenant-id $LURAN_TENANT_ID

# Script will show which nodes have broken references
# Re-run creation scripts or fix manually
```

---

## Next Steps

### 1. Integrate with Chat Widget ✅

Add Luran support flows to your customer-facing widget:

```javascript
// Widget configuration
<script>
  window.LuranWidget = {
    tenantId: 'your-luran-support-tenant-id',
    apiKey: 'your-public-api-key',
    position: 'bottom-right',
    enableFlows: true  // Enable flow support
  };
</script>
<script src="https://cdn.luran.ai/widget.js"></script>
```

### 2. Configure Escalations ✅

Set up escalation routing so ESCALATE nodes create tickets:

```python
# In your ticket system integration
@app.post("/api/v1/escalations")
async def handle_escalation(
    subject: str,
    reason: str,
    session_id: str,
    user_info: dict
):
    # Create ticket in your system
    ticket = create_support_ticket(
        subject=subject,
        description=reason,
        user=user_info,
        context=get_flow_context(session_id)
    )
    return {"ticket_id": ticket.id}
```

### 3. Monitor Analytics ✅

Track flow performance:

```sql
-- Daily flow usage
SELECT
  f.name,
  DATE(fs.created_at) as date,
  COUNT(*) as sessions,
  AVG(CASE WHEN fs.status = 'completed' THEN 1.0 ELSE 0.0 END) as completion_rate
FROM flows f
JOIN flow_sessions fs ON f.id = fs.flow_id
WHERE f.tenant_id = 'your-tenant-id'
  AND fs.created_at > NOW() - INTERVAL '7 days'
GROUP BY f.id, f.name, DATE(fs.created_at)
ORDER BY date DESC, sessions DESC;
```

### 4. Refine Based on Data ✅

After 1 week of usage:

**Check completion rates:**
- Target: 70%+ completion
- If lower: Identify drop-off points and simplify

**Review escalations:**
- Which subjects appear most?
- Add flows for common escalation topics

**Update trigger keywords:**
- What phrases do users actually use?
- Add synonyms and variations

**Improve messages:**
- A/B test different wording
- Clarify confusing instructions
- Add examples where helpful

### 5. Expand Content ✅

Add more flows for:
- New features you launch
- Seasonal topics (e.g., year-end billing)
- Regional variations (multi-language)
- Advanced user segments (developers vs business users)

---

## Success Metrics

Track these KPIs weekly:

| Metric | Target | How to Measure |
|--------|--------|----------------|
| Flow Completion Rate | 70%+ | `completed_sessions / total_sessions` |
| Escalation Rate | <30% | `escalated_sessions / total_sessions` |
| Average Resolution Time | Decreasing | `AVG(end_time - start_time)` |
| User Satisfaction | 4.0+ / 5.0 | Post-flow survey |
| Deflection Rate | 50%+ | `(completed_without_escalation / total_sessions)` |

---

## Resources

### Documentation
- **Implementation Guide:** `docs/LURAN_SUPPORT_FLOWS_IMPLEMENTATION.md`
- **Node Reference:** `docs/LURAN_FLOWS_NODE_REFERENCE.md`
- **Flow Architecture:** `docs/NESTED_FLOW_EXPLAINED.md`

### Scripts
- **Create Part 1:** `scripts/create_luran_support_flows.py`
- **Create Part 2:** `scripts/create_luran_support_flows_part2.py`
- **Validate:** `scripts/test_luran_flows.py`

### Support
- GitHub Issues: [github.com/luran/luran/issues](https://github.com/luran/luran/issues)
- Documentation: [docs.luran.ai](https://docs.luran.ai)
- Email: support@luran.ai

---

## Cheat Sheet

```bash
# Set tenant ID
export LURAN_TENANT_ID="abc-123-def-456"

# Create all flows
python scripts/create_luran_support_flows.py --tenant-id $LURAN_TENANT_ID
python scripts/create_luran_support_flows_part2.py --tenant-id $LURAN_TENANT_ID

# Validate
python scripts/test_luran_flows.py --tenant-id $LURAN_TENANT_ID

# Test via API
curl -X POST http://localhost:8000/api/v1/chat/message \
  -H "Content-Type: application/json" \
  -H "X-Tenant-ID: $LURAN_TENANT_ID" \
  -H "X-API-Key: $API_KEY" \
  -d '{"message": "getting started", "user_identifier": "test@example.com"}'

# Check flows in database
psql -d luran_db -c "
SELECT name, COUNT(fn.id) as nodes
FROM flows f
JOIN flow_nodes fn ON f.id = fn.flow_id
WHERE f.tenant_id = '$LURAN_TENANT_ID'
GROUP BY f.id, name;
"

# Monitor usage
psql -d luran_db -c "
SELECT f.name, COUNT(DISTINCT fs.session_id) as sessions
FROM flows f
LEFT JOIN flow_sessions fs ON f.id = fs.flow_id
WHERE f.tenant_id = '$LURAN_TENANT_ID'
GROUP BY f.name
ORDER BY sessions DESC;
"
```

---

## Troubleshooting Checklist

When things go wrong, check these in order:

1. **Database connection** - Can you connect to PostgreSQL?
2. **Tenant exists** - Does the tenant ID exist in the database?
3. **Flows created** - Do you have 5 flows for your tenant?
4. **Knowledge uploaded** - Is your knowledge base populated?
5. **Validation passed** - Does the test script show all green?
6. **API accessible** - Can you reach the chat endpoint?
7. **Trigger keywords** - Do your test messages match trigger keywords?
8. **Node references** - Are all next_node_id references valid?

If all checks pass and flows still don't work, check application logs:
```bash
tail -f logs/luran.log | grep -i flow
```

---

## Quick Reference: Trigger Keywords

Use these phrases to trigger each flow:

**Flow 1: Getting Started**
- "getting started"
- "how do I start"
- "setup"
- "onboarding"

**Flow 2: Knowledge Upload Issues**
- "upload problem"
- "file won't upload"
- "knowledge base error"
- "processing stuck"

**Flow 3: Widget Integration Issues**
- "widget not working"
- "CORS error"
- "integration problem"
- "widget won't load"

**Flow 4: Account & Subscription**
- "upgrade"
- "pricing"
- "trial expired"
- "billing"

**Flow 5: Troubleshooting & Errors**
- "error"
- "not working"
- "bug"
- "problem"

---

**You're all set!** 🎉

Your Luran support flows are ready to handle customer inquiries automatically.

Monitor the analytics, gather feedback, and iterate to continuously improve your support experience.

---

*Last updated: 2026-02-06*
