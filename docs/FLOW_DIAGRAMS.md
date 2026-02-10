# Luran Support Flows - Visual Diagrams

Visual representation of all 5 support flows with their branching structures.

---

## Flow 1: Getting Started (Onboarding)

### High-Level Structure
```
START → Welcome → "Where are you in setup?" →
├─ Just signed up (20 nodes)
│  └─ Email verify → Knowledge upload → Widget setup → Testing
├─ Uploaded knowledge (12 nodes)
│  └─ Widget setup → Testing
├─ Integrated widget (10 nodes)
│  └─ Testing → Quality checks → Upgrade info
└─ Already live (6 nodes)
   └─ Optimization options
```

### Detailed: "Just Signed Up" Branch
```
"Just signed up" →
  Email verified? →
    ├─ Yes →
    │   Content type? →
    │     ├─ Documents →
    │     │   Upload guide →
    │     │   Success? →
    │     │     ├─ Yes → [Widget Setup]
    │     │     └─ Error →
    │     │         Error type? →
    │     │           ├─ File too large → RAG: limits → Upgrade tip
    │     │           ├─ Unsupported → Format guide
    │     │           └─ Processing failed → TEXT_INPUT → ESCALATE
    │     ├─ Website → Scraping guide → [Widget Setup]
    │     └─ Manual → Entry guide → [Widget Setup]
    ├─ No → Verification help → END
    └─ Can't find → ESCALATE
```

### Detailed: Widget Setup Branch
```
Widget Setup →
  Platform? →
    ├─ HTML →
    │   HTML guide →
    │   Working? →
    │     ├─ Yes → [Testing]
    │     ├─ Not appearing → Troubleshooting → ESCALATE
    │     └─ CORS → CORS fix → RAG → Success? → [Testing]/ESCALATE
    ├─ React → React guide → [Testing]
    ├─ WordPress → WP guide → [Testing]
    └─ Shopify → Shopify guide → [Testing]
```

### Detailed: Testing Branch
```
Testing →
  AI performance? →
    ├─ Correct → Upgrade info → RAG: plans → END ✓
    ├─ Doesn't know →
    │   Issue type? →
    │     ├─ Says "I don't know" → Indexing check → END
    │     ├─ Wrong answers → Improvement tips → END
    │     └─ Other → ESCALATE
    └─ Errors → TEXT_INPUT → ESCALATE
```

**Total Nodes:** 48
**Max Depth:** 7 levels
**Escalation Points:** 6

---

## Flow 2: Knowledge Upload Issues

### High-Level Structure
```
START → Diagnostic → "What's happening?" →
├─ Upload fails (12 nodes)
│  └─ Error type → Solutions/Escalate
├─ Processing fails (10 nodes)
│  └─ File status → Troubleshoot/Escalate
├─ Stuck processing (8 nodes)
│  └─ Time check → Wait/Escalate
└─ Missing files (8 nodes)
   └─ Success confirm → Refresh/Retry
```

### Detailed: Upload Fails Branch
```
Upload fails →
  Error message? →
    ├─ File too large →
    │   File size issue →
    │   RAG: limits →
    │   Resolution? →
    │     ├─ Split file → Splitting guide → END
    │     ├─ Upgrade → RAG: upgrade → END
    │     └─ Compress → Compression tips → END
    │
    ├─ Unsupported format →
    │   Format type? →
    │     ├─ Excel → Convert CSV → END
    │     ├─ Image → OCR guide → END
    │     ├─ PowerPoint → Export PDF → END
    │     └─ Other → TEXT_INPUT → ESCALATE
    │
    ├─ Storage limit →
    │   RAG: storage →
    │   Resolution? →
    │     ├─ Delete old → Deletion guide → END
    │     ├─ Upgrade → RAG: upgrade → END
    │     └─ Need info → ESCALATE
    │
    └─ Other → TEXT_INPUT → ESCALATE
```

### Detailed: Processing Fails Branch
```
Processing fails →
  File status? →
    ├─ Failed →
    │   File type? →
    │     ├─ PDF → PDF troubleshooting → END
    │     ├─ DOCX → DOCX guide → END
    │     └─ Other → TEXT_INPUT → ESCALATE
    │
    └─ Partially → Partial processing msg → END
```

### Detailed: Stuck Processing Branch
```
Stuck processing →
  How long? →
    ├─ Under 5 min → Wait message → END
    ├─ 5-15 min → Refresh guide → ESCALATE
    └─ Over 15 min → ESCALATE (urgent)
```

### Detailed: Missing Files Branch
```
Missing files →
  Success message? →
    ├─ Yes → Refresh guide → ESCALATE
    ├─ No → Retry message → END
    └─ Not sure → Dashboard check → END
```

**Total Nodes:** 38
**Max Depth:** 5 levels
**Escalation Points:** 8

---

## Flow 3: Widget Integration Issues

### High-Level Structure
```
START → Troubleshooting → "What's wrong?" →
├─ Widget doesn't appear (15 nodes)
│  └─ Platform-specific guides
├─ CORS error (10 nodes)
│  └─ Deep CORS troubleshooting
├─ Widget broken (12 nodes)
│  └─ Functionality diagnostics
└─ Configuration (5 nodes)
   └─ Find credentials
```

### Detailed: Widget Not Showing Branch
```
Widget not showing →
  Platform? →
    ├─ HTML →
    │   HTML troubleshooting →
    │   Checklist? →
    │     ├─ Script in <head> → Move script guide → END
    │     ├─ Wrong tenant_id → Tenant ID guide → END
    │     ├─ Console errors → TEXT_INPUT → Error handler → ESCALATE
    │     └─ Checked all → ESCALATE
    │
    ├─ React → React troubleshooting → END
    └─ Other → Generic troubleshooting → END
```

### Detailed: CORS Error Branch
```
CORS error →
  CORS fix message →
  RAG: CORS config →
  Fixed? →
    ├─ Yes → Success → END ✓
    └─ Still error →
        Advanced troubleshooting →
        Domain format? →
          ├─ localhost:3000 → Localhost guide → END
          ├─ No protocol → Protocol guide → END
          ├─ Subdomain → Subdomain guide → END
          └─ Added correctly → TEXT_INPUT → ESCALATE
```

### Detailed: Widget Broken Branch
```
Widget broken →
  What's not working? →
    ├─ Can't send →
    │   Send behavior? →
    │     ├─ Nothing happens → Console check → TEXT_INPUT → ESCALATE
    │     ├─ Error appears → TEXT_INPUT → ESCALATE
    │     └─ Sends, no response → [No Response flow]
    │
    ├─ No AI response →
    │   Chat display? →
    │     ├─ Loading forever → Status check → END
    │     ├─ Error message → TEXT_INPUT → ESCALATE
    │     └─ Blank → ESCALATE
    │
    └─ Crashes → TEXT_INPUT → ESCALATE
```

### Detailed: Configuration Branch
```
Configuration →
  Missing what? →
    ├─ Tenant ID → RAG: find tenant ID → END
    ├─ Public key → Public key guide → END
    ├─ Secret key → Secret key guide → END
    └─ Widget code → Widget code guide → END
```

**Total Nodes:** 42
**Max Depth:** 6 levels
**Escalation Points:** 8

---

## Flow 4: Account & Subscription

### High-Level Structure
```
START → Support → "What do you need?" →
├─ Trial info (12 nodes)
│  └─ RAG → Trial questions
├─ Upgrade (20 nodes)
│  └─ Plan selection → Details
├─ Billing (25 nodes)
│  └─ Payment/Invoice/Cancel
├─ Plan comparison (10 nodes)
│  └─ RAG → Selection
└─ Account access (5 nodes)
   └─ Login/Verification issues
```

### Detailed: Trial Info Branch
```
Trial info →
  RAG: trial →
  Trial question? →
    ├─ Duration → Duration message → END
    ├─ Expired →
    │   Action? →
    │     ├─ See plans → [Plan Comparison]
    │     ├─ Upgrade now → [Upgrade]
    │     ├─ Not ready → Data saved → END
    │     └─ Need time → ESCALATE (extension)
    ├─ Limitations → Limits message → RAG: vs paid → END
    └─ Extend → ESCALATE (extension)
```

### Detailed: Upgrade Branch
```
Upgrade →
  RAG: plans →
  Plan interest? →
    ├─ Lite ($29) →
    │   Lite details →
    │   Fits needs? →
    │     ├─ Yes → Upgrade instructions → END
    │     ├─ Need more → [Plan Comparison]
    │     └─ Questions → TEXT_INPUT → ESCALATE
    │
    ├─ Professional ($99) →
    │   Pro details →
    │   Ready? →
    │     ├─ Yes → Upgrade instructions → END
    │     ├─ Compare Growth → [Growth Plan]
    │     └─ Questions → TEXT_INPUT → RAG → END
    │
    ├─ Growth ($199) →
    │   Growth details →
    │   Right plan? →
    │     ├─ Yes → Upgrade instructions → END
    │     ├─ Need more → Enterprise → ESCALATE
    │     └─ Compare all → [Plan Comparison]
    │
    ├─ Enterprise → Enterprise message → ESCALATE (sales)
    └─ Not sure → [Plan Comparison]
```

### Detailed: Billing Branch
```
Billing →
  Issue type? →
    ├─ Payment failed →
    │   Error? →
    │     ├─ Card declined → Card declined tips → END
    │     ├─ Paystack error → TEXT_INPUT → ESCALATE
    │     ├─ Paid but not upgraded → ESCALATE (urgent)
    │     └─ Other → TEXT_INPUT → ESCALATE
    │
    ├─ Invoice →
    │   Type? →
    │     ├─ Resend last → Email check → END
    │     ├─ Specific month → Dashboard guide → END
    │     └─ Custom details → TEXT_INPUT → ESCALATE
    │
    ├─ Cancel →
    │   Reason? →
    │     ├─ Too expensive →
    │     │   Downgrade offer →
    │     │   Downgrade? →
    │     │     ├─ Yes → Downgrade guide → END
    │     │     └─ No → [Cancel Process]
    │     ├─ Not using →
    │     │   Pause suggestion →
    │     │   Pause? →
    │     │     ├─ Yes → Downgrade Lite → END
    │     │     └─ No → [Cancel Process]
    │     ├─ Technical → TEXT_INPUT → ESCALATE
    │     └─ Just cancel → [Cancel Process]
    │         Cancel instructions →
    │         Feedback? →
    │           ├─ Yes → TEXT_INPUT → ESCALATE
    │           └─ No → Thank you → END
    │
    └─ Change payment → Payment method guide → END
```

### Detailed: Account Access Branch
```
Account access →
  Issue? →
    ├─ Forgot password → Reset guide → END
    ├─ Locked → ESCALATE (security)
    ├─ Email verify → Verification check → ESCALATE
    └─ Other → TEXT_INPUT → ESCALATE
```

**Total Nodes:** 72
**Max Depth:** 7 levels
**Escalation Points:** 12

---

## Flow 5: Troubleshooting & Errors

### High-Level Structure
```
START → Diagnostic → "Problem area?" →
├─ Chat/AI (30 nodes)
│  └─ Wrong answers/No knowledge/Slow/Rate limit
├─ Knowledge → Redirect to Flow 2 (2 nodes)
├─ Widget → Redirect to Flow 3 (2 nodes)
├─ Dashboard (20 nodes)
│  └─ Login/Loading/Features/Data
├─ API (25 nodes)
│  └─ Auth/Rate limit/Server errors/Docs
└─ Not sure (6 nodes)
   └─ General diagnostic
```

### Detailed: Chat Issues Branch
```
Chat issues →
  Issue type? →
    ├─ Wrong answers →
    │   Type? →
    │     ├─ Outdated → Update knowledge guide → END
    │     ├─ Wrong context → RAG: improve retrieval → END
    │     ├─ Unrelated → ESCALATE (quality)
    │     └─ Mixed topics → Section headers tip → END
    │
    ├─ Says "I don't know" →
    │   Uploaded knowledge? →
    │     ├─ Yes → Processing check → RAG: not retrieving → END
    │     ├─ No → Upload guide → END
    │     └─ Not sure → Dashboard check → END
    │
    ├─ Too slow →
    │   Speed? →
    │     ├─ 5-10 sec → Normal slowness → END
    │     ├─ 10-30 sec → RAG: optimize → ESCALATE
    │     ├─ Over 30 sec → ESCALATE (timeout - urgent)
    │     └─ Intermittent → TEXT_INPUT → ESCALATE
    │
    └─ Rate limit →
        RAG: limits →
        Resolution? →
          ├─ Upgrade → Upgrade redirect → END
          ├─ Wait → Reset date guide → END
          ├─ Temp increase → ESCALATE
          └─ Check usage → Usage dashboard → END
```

### Detailed: Dashboard Issues Branch
```
Dashboard issues →
  Problem? →
    ├─ Can't login → Login redirect → END
    │
    ├─ Won't load →
    │   Cache clear guide → ESCALATE
    │
    ├─ Feature missing →
    │   Which feature? →
    │     ├─ Flows → RAG: flows feature → Plan redirect
    │     ├─ Analytics → Analytics plan message → Plan redirect
    │     ├─ White-label → White-label plan → Plan redirect
    │     └─ Other → TEXT_INPUT → ESCALATE
    │
    └─ Data not updating → Refresh guide → ESCALATE
```

### Detailed: API Issues Branch
```
API issues →
  Error? →
    ├─ 401 Unauthorized →
    │   Auth problem? →
    │     ├─ Invalid key → Regenerate guide → END
    │     ├─ Key not working →
    │     │   Key type? →
    │     │     ├─ Public → Public usage guide → END
    │     │     └─ Secret → Secret usage guide → END
    │     ├─ tenant_id → Tenant ID location → END
    │     └─ Other → TEXT_INPUT → ESCALATE
    │
    ├─ 429 Rate limit → [Rate Limit Handler from Chat]
    │
    ├─ 500 Server error → TEXT_INPUT → ESCALATE (urgent)
    │
    ├─ Endpoint issue → TEXT_INPUT → ESCALATE
    │
    └─ Need docs → RAG: API docs → Docs link → END
```

### Detailed: General Diagnostic Branch
```
Not sure →
  TEXT_INPUT: "What were you trying to do?" →
  TEXT_INPUT: "What error?" →
  Where? →
    ├─ Dashboard → [Dashboard Issues]
    ├─ Widget → Redirect Flow 3
    ├─ API → [API Issues]
    └─ Other → ESCALATE (general)
```

**Total Nodes:** 85
**Max Depth:** 6 levels
**Escalation Points:** 12

---

## Cross-Flow Navigation Map

```
┌──────────────────────────────────────────────────────────────┐
│                    Flow 5: Troubleshooting                   │
│                    (Central Routing Hub)                      │
└────────────┬─────────────────┬────────────────┬──────────────┘
             │                 │                │
             │                 │                │
    Knowledge│        Widget   │     Account/  │
      issues │      issues     │       Billing │
             │                 │                │
             ▼                 ▼                ▼
    ┌────────────┐   ┌─────────────┐   ┌─────────────┐
    │  Flow 2    │   │   Flow 3    │   │   Flow 4    │
    │  Knowledge │   │   Widget    │   │   Account   │
    └────────────┘   └─────────────┘   └─────────────┘
             │                 │                │
             └─────────────────┴────────────────┘
                              │
                              ▼
                    ┌─────────────────┐
                    │    Flow 1       │
                    │ Getting Started │
                    │ (References all)│
                    └─────────────────┘
```

### Flow Interconnections

**Flow 1 → Other Flows:**
- References knowledge upload (Flow 2 topics)
- References widget setup (Flow 3 topics)
- Mentions upgrade paths (Flow 4)

**Flow 5 → Other Flows:**
- Redirects knowledge issues → Flow 2
- Redirects widget issues → Flow 3
- Redirects account/billing → Flow 4

**All Flows → Flow 4:**
- Upgrade suggestions
- Plan comparisons
- Trial expiration handling

---

## Node Type Distribution by Flow

```
                        START MSG  QUES RAG  TEXT ESC  END  TOTAL
Flow 1: Getting Started   1   12    8   5    3   6    13    48
Flow 2: Knowledge Upload  1    9    7   3    4   8     6    38
Flow 3: Widget Integration 1   11    8   2    5   8     7    42
Flow 4: Account & Subs    1   20   15   6    7  12    11    72
Flow 5: Troubleshooting   1   33   32   9   11  12    17    85
─────────────────────────────────────────────────────────────────
TOTAL:                    5   85   70  25   30  46    54   285
```

### Visual Breakdown

```
MESSAGE (85):       ███████████████████████████████ 30%
QUESTION (70):      ██████████████████████████ 25%
END (54):           ███████████████████ 19%
ESCALATE (46):      ████████████████ 16%
TEXT_INPUT (30):    ██████████ 11%
RAG_QUERY (25):     ████████ 9%
START (5):          █ <1%
```

---

## Depth Analysis

### Maximum Path Lengths

**Flow 1: Getting Started**
```
Longest Path (7 levels):
START → Welcome → Setup Q → Just signed up → Email verify Q →
Knowledge upload → Content Q → Documents → Upload Q → Error Q →
File size → RAG
```

**Flow 2: Knowledge Upload**
```
Longest Path (5 levels):
START → Diagnostic → Problem Q → Upload fails → Error Q →
File too large → Resolution Q → Split file
```

**Flow 3: Widget Integration**
```
Longest Path (6 levels):
START → Troubleshooting → Issue Q → CORS error → CORS fix →
RAG → Success Q → Advanced → Domain Q → Guide
```

**Flow 4: Account & Subscription**
```
Longest Path (7 levels):
START → Support → Help Q → Billing → Issue Q → Cancel →
Reason Q → Too expensive → Downgrade Q → No → Cancel →
Feedback Q → TEXT_INPUT → ESCALATE
```

**Flow 5: Troubleshooting**
```
Longest Path (6 levels):
START → Diagnostic → Area Q → Not sure → TEXT_INPUT (what) →
TEXT_INPUT (error) → Where Q → ESCALATE
```

### Average Depth by Flow

```
Flow 1: ████████░░ 4.2 levels avg
Flow 2: ███████░░░ 3.5 levels avg
Flow 3: ████████░░ 4.0 levels avg
Flow 4: █████████░ 4.5 levels avg
Flow 5: ████████░░ 4.0 levels avg

Overall Average: 4.0 levels
```

---

## Escalation Path Map

### By Subject Category

```
Technical Issues (15 escalations):
├─ Widget crashes
├─ Processing failures
├─ API server errors
├─ CORS errors (persistent)
├─ Chat timeout
├─ Dashboard access
├─ Data sync issues
└─ Quality issues

Billing & Payments (8 escalations):
├─ Payment failures
├─ Paystack errors
├─ Payment processing discrepancies
├─ Custom invoice requests
├─ Cancellation with feedback
└─ Refund requests

Account Access (4 escalations):
├─ Account locked
├─ Email verification
├─ Security concerns
└─ Access issues

Sales Inquiries (3 escalations):
├─ Enterprise plans
├─ Trial extensions
├─ Custom contracts

Feature Requests (5 escalations):
├─ Feature access questions
├─ Plan selection help
├─ Temporary limit increases
├─ Custom integrations
└─ Special requirements

General Support (11 escalations):
├─ Unresolved technical issues
├─ Complex troubleshooting
├─ Format conversion help
├─ Storage planning
└─ General diagnostics
```

---

## Trigger Keyword Coverage

### Keyword to Flow Mapping

```
"getting started" ──────────┐
"how do I start" ───────────┤
"setup" ────────────────────┼──→ Flow 1: Getting Started
"onboarding" ───────────────┤
"begin", "start" ───────────┘

"upload problem" ───────────┐
"file won't upload" ────────┤
"knowledge base error" ─────┼──→ Flow 2: Knowledge Upload
"processing stuck" ─────────┤
"can't upload" ─────────────┘

"widget not working" ───────┐
"CORS error" ───────────────┤
"integration problem" ──────┼──→ Flow 3: Widget Integration
"widget won't load" ────────┤
"iframe issue" ─────────────┘

"upgrade" ──────────────────┐
"pricing" ──────────────────┤
"trial expired" ────────────┼──→ Flow 4: Account & Subscription
"billing" ──────────────────┤
"subscription", "payment" ──┘

"error" ────────────────────┐
"not working" ──────────────┤
"bug" ──────────────────────┼──→ Flow 5: Troubleshooting
"problem" ──────────────────┤
"broken", "issue" ──────────┘
```

### Keyword Conflict Resolution

No conflicts - Each keyword uniquely maps to one flow.

**Priority order if needed:**
1. Most specific keywords (e.g., "CORS error" → Flow 3)
2. Moderate specific (e.g., "upload problem" → Flow 2)
3. Generic (e.g., "error" → Flow 5)

---

## Success Path Examples

### Happy Path 1: New User Complete Setup
```
Flow 1 Start
→ "getting started"
→ "Just signed up"
→ "Yes" (email verified)
→ "Documents (PDF, DOCX, TXT)"
→ "Success! Files uploaded"
→ "Custom HTML/JavaScript"
→ "Yes, it's working!"
→ "AI answers correctly!"
→ END (with upgrade info) ✓
```
**Steps:** 8 | **Time:** ~3-5 minutes

### Happy Path 2: CORS Error Resolution
```
Flow 3 Start
→ "CORS error"
→ (CORS fix instructions + RAG)
→ "Yes, working now!"
→ END ✓
```
**Steps:** 3 | **Time:** ~1-2 minutes

### Happy Path 3: Plan Upgrade
```
Flow 4 Start
→ "upgrade"
→ (RAG: plans)
→ "Professional ($99/mo)"
→ "Yes" (ready to upgrade)
→ (Upgrade instructions)
→ END ✓
```
**Steps:** 5 | **Time:** ~2-3 minutes

---

## Abandonment Risk Points

### High-Risk Drop-off Nodes

**Flow 1:**
- After email verification (if user hasn't verified)
- During upload if errors occur repeatedly
- CORS configuration (if too complex)

**Flow 2:**
- Format conversion steps (if user unsure how)
- Waiting for processing (if too long)

**Flow 3:**
- Advanced CORS troubleshooting (multiple attempts)
- Console error interpretation (technical barrier)

**Flow 4:**
- Plan comparison (decision fatigue)
- Cancellation flow (retention attempts)

**Flow 5:**
- General diagnostic (requires text input)
- Multiple failed attempts at fixing

### Mitigation Strategies

1. **Offer early exit:** Clear "Talk to support" option
2. **Progress indicators:** Show where user is in flow
3. **Simplify steps:** Break complex tasks into smaller chunks
4. **Provide examples:** Show what good input looks like
5. **Quick wins:** Resolve easy issues fast to build confidence

---

*Visual reference for Luran Support Flows*
*Last updated: 2026-02-06*
