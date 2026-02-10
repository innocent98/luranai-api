# Luran Support Flows - Node Configuration Reference

Quick reference for all node types and their configurations in the Luran support flows.

---

## Node Types Used

### 1. START
**Purpose:** Entry point for each flow
**Config:**
```json
{
  "next_node_id": "welcome_msg"
}
```

---

### 2. MESSAGE
**Purpose:** Display information to user
**Config:**
```json
{
  "message": "Text message to display",
  "next_node_id": "next_node_identifier"
}
```

**Examples from flows:**

```json
// Welcoming message
{
  "message": "👋 Welcome to Luran! I'll help you get set up with your AI-powered customer support. Let's get started!",
  "next_node_id": "setup_stage_q"
}

// Instructional message
{
  "message": "📝 To upload documents:\n\n1. Go to Dashboard → Knowledge Base\n2. Click 'Upload Files'\n3. Select your PDF, DOCX, or TXT files\n4. Wait for processing to complete\n\nMax file size varies by plan.",
  "next_node_id": "upload_success_q"
}

// Troubleshooting message
{
  "message": "🔧 Common HTML widget issues:\n\n1. Script in <head> instead of <body>\n2. Incorrect tenant_id\n3. Domain not in Allowed Origins\n4. Browser cache",
  "next_node_id": "html_checklist_q"
}
```

---

### 3. QUESTION
**Purpose:** Present multiple choice options to user (rendered as buttons)
**Config:**
```json
{
  "question": "Question text?",
  "options": [
    {"text": "Option 1", "next_node_id": "node_for_option_1"},
    {"text": "Option 2", "next_node_id": "node_for_option_2"},
    {"text": "Option 3", "next_node_id": "node_for_option_3"}
  ]
}
```

**Examples from flows:**

```json
// Simple binary choice
{
  "question": "Did that fix the CORS error?",
  "options": [
    {"text": "Yes, working now!", "next_node_id": "cors_success"},
    {"text": "Still getting CORS error", "next_node_id": "cors_advanced"}
  ]
}

// Multiple path selection
{
  "question": "Where are you in the setup process?",
  "options": [
    {"text": "Just signed up", "next_node_id": "account_setup_branch"},
    {"text": "Uploaded knowledge", "next_node_id": "widget_setup_branch"},
    {"text": "Integrated widget", "next_node_id": "testing_branch"},
    {"text": "Already live", "next_node_id": "optimization_branch"}
  ]
}

// Troubleshooting selection
{
  "question": "What error did you see?",
  "options": [
    {"text": "File too large", "next_node_id": "file_size_issue"},
    {"text": "Unsupported format", "next_node_id": "format_issue"},
    {"text": "Storage limit exceeded", "next_node_id": "storage_limit_issue"},
    {"text": "Other error", "next_node_id": "other_error_input"}
  ]
}
```

---

### 4. RAG_QUERY
**Purpose:** Retrieve information from knowledge base using AI
**Config:**
```json
{
  "query_template": "Search query for knowledge base",
  "next_node_id": "success_node",
  "fallback_node_id": "escalation_node"
}
```

**Examples from flows:**

```json
// Plan information
{
  "query_template": "subscription plans pricing features comparison all plans",
  "next_node_id": "plan_interest_q",
  "fallback_node_id": "upgrade_escalate"
}

// Technical documentation
{
  "query_template": "CORS allowed origins configuration setup",
  "next_node_id": "cors_fix_success_q",
  "fallback_node_id": "cors_advanced"
}

// Limits and restrictions
{
  "query_template": "storage limits by plan how much storage",
  "next_node_id": "storage_resolution_q",
  "fallback_node_id": "storage_escalate"
}

// Troubleshooting guidance
{
  "query_template": "improve retrieval accuracy AI answering from wrong context",
  "next_node_id": "end_chat",
  "fallback_node_id": "quality_escalate"
}

// Feature availability
{
  "query_template": "flows feature which plan includes flows",
  "next_node_id": "plan_redirect_msg",
  "fallback_node_id": "plan_redirect_msg"
}
```

**Best Practices for RAG Queries:**
- Use specific, targeted queries
- Include synonyms and related terms
- Provide fallback for when information isn't found
- Keep queries focused on one topic

---

### 5. TEXT_INPUT
**Purpose:** Collect free-form text input from user
**Config:**
```json
{
  "prompt": "Question or instruction for user",
  "placeholder": "Example text...",
  "next_node_id": "node_to_process_input"
}
```

**Examples from flows:**

```json
// Error details collection
{
  "prompt": "Please paste the exact error message you're seeing:",
  "placeholder": "Paste error message here...",
  "next_node_id": "other_error_escalate"
}

// Feedback collection
{
  "prompt": "We'd love to hear your feedback:",
  "placeholder": "Tell us what we could improve...",
  "next_node_id": "feedback_escalate"
}

// Problem description
{
  "prompt": "Please describe what you were trying to do:",
  "placeholder": "I was trying to...",
  "next_node_id": "error_message_input_general"
}

// Feature request
{
  "prompt": "What's your main consideration in choosing a plan?",
  "placeholder": "e.g., 'Budget', 'Number of messages', 'Team size'",
  "next_node_id": "plan_selection_escalate"
}
```

**Use Cases:**
- Collecting error messages for escalation
- Gathering detailed problem descriptions
- User feedback collection
- Custom requirements that don't fit predefined options

---

### 6. ESCALATE
**Purpose:** Hand off to human support with context
**Config:**
```json
{
  "subject": "Ticket subject line",
  "reason": "Why this was escalated",
  "end_flow": true
}
```

**Examples from flows:**

```json
// Technical issue escalation
{
  "subject": "Widget not appearing - HTML",
  "reason": "User needs advanced widget troubleshooting",
  "end_flow": true
}

// Billing escalation
{
  "subject": "Payment processing - urgent",
  "reason": "Payment succeeded but plan not upgraded",
  "end_flow": true
}

// General inquiry escalation
{
  "subject": "Enterprise sales inquiry",
  "reason": "User interested in Enterprise plan",
  "end_flow": true
}

// Complex troubleshooting
{
  "subject": "AI answer quality issue",
  "reason": "AI providing poor quality answers",
  "end_flow": true
}

// Account security
{
  "subject": "Account locked - security",
  "reason": "User's account is locked",
  "end_flow": true
}
```

**Escalation Categories:**

1. **Technical Issues**
   - Widget crashes
   - Processing failures
   - API errors
   - CORS issues

2. **Billing & Payments**
   - Payment failures
   - Refund requests
   - Invoice discrepancies
   - Plan upgrade issues

3. **Account Access**
   - Locked accounts
   - Verification problems
   - Security concerns

4. **Sales Inquiries**
   - Enterprise plans
   - Custom contracts
   - Large-scale deployments

5. **Feature Requests**
   - Missing functionality
   - Custom integrations
   - Special requirements

---

### 7. END
**Purpose:** Gracefully end the flow
**Config:**
```json
{
  "message": "Closing message",
  "offer_restart": true | false
}
```

**Examples from flows:**

```json
// Successful completion
{
  "message": "Welcome to Luran! If you need anything else, just ask. 🚀",
  "offer_restart": false
}

// Help offered
{
  "message": "Hope that helps! Let me know if you need more assistance.",
  "offer_restart": true
}

// Partial completion
{
  "message": "Feel free to try uploading again, or type 'upgrade' to see plan options!",
  "offer_restart": true
}

// Neutral ending
{
  "message": "Let me know if you still can't find your files!",
  "offer_restart": true
}

// Final goodbye
{
  "message": "Thank you for using Luran! We hope to see you again. 💙",
  "offer_restart": false
}
```

**When to offer_restart:**
- `true`: User might need more help with related issues
- `false`: Natural conclusion, user achieved their goal

---

## Common Patterns

### Pattern 1: Diagnostic Flow
```
MESSAGE (explain issue) →
QUESTION (multiple choice) →
  ├─ Option 1 → MESSAGE (solution) → END
  ├─ Option 2 → RAG_QUERY → MESSAGE → END
  └─ Option 3 → TEXT_INPUT → ESCALATE
```

### Pattern 2: Progressive Assistance
```
MESSAGE (welcome) →
QUESTION (what stage are you at?) →
  ├─ Stage 1 → MESSAGE (instructions) → QUESTION (success?) →
  │              ├─ Yes → Next Stage
  │              └─ No → TROUBLESHOOT
  ├─ Stage 2 → ...
  └─ Stage 3 → ...
```

### Pattern 3: Escalation Path
```
MESSAGE (diagnostic) →
QUESTION (category selection) →
  ├─ Simple issue → MESSAGE (fix) → END
  ├─ Medium issue → RAG_QUERY → MESSAGE → END
  └─ Complex issue → TEXT_INPUT (details) → ESCALATE
```

### Pattern 4: Information Retrieval
```
MESSAGE (intro) →
RAG_QUERY (get info) →
  ├─ Success → MESSAGE (display info) → QUESTION (follow-up)
  └─ Fallback → ESCALATE
```

### Pattern 5: Multi-Path Resolution
```
QUESTION (what's wrong?) →
  ├─ Issue A → [Diagnostic subflow for A]
  ├─ Issue B → [Diagnostic subflow for B]
  ├─ Issue C → [Redirect to specialized flow]
  └─ Not sure → TEXT_INPUT → GENERAL_DIAGNOSTIC
```

---

## Node Naming Conventions

### Descriptive Names
- Use clear, descriptive node identifiers
- Include action/purpose in name
- Use underscores for readability

**Good examples:**
- `welcome_msg`
- `setup_stage_q`
- `trial_expired_handler`
- `cors_fix_success_q`
- `upload_error_escalate`

**Poor examples:**
- `node1`
- `msg`
- `question`
- `next`

### Naming Patterns

**Messages:**
- `{topic}_msg` - General message
- `{topic}_info` - Informational message
- `{topic}_guide` - Instructional message
- `{topic}_tip` - Helpful tip

**Questions:**
- `{topic}_q` - General question
- `{topic}_type_q` - Category selection
- `{status}_q` - Status check

**Actions:**
- `{action}_handler` - Processing node
- `{topic}_branch` - Branch point
- `{action}_check` - Verification node

**Inputs:**
- `{topic}_input` - Text input collection
- `{topic}_rag` - RAG query node

**Endpoints:**
- `{topic}_escalate` - Escalation
- `end_{topic}` - Flow termination
- `{topic}_redirect_msg` - Redirect to another flow

---

## Flow Interconnections

### Cross-Flow References

Some flows redirect to others:

**Flow 5 (Troubleshooting) → Other Flows:**
```json
// Redirect to Flow 2 (Knowledge Upload)
{
  "message": "For knowledge base issues, type: 'upload problem'\n\nI have a specialized flow for upload and processing issues!",
  "next_node_id": "end_redirect"
}

// Redirect to Flow 3 (Widget Integration)
{
  "message": "For widget integration issues, type: 'widget not working'\n\nI have a specialized flow for widget and CORS issues!",
  "next_node_id": "end_redirect"
}

// Redirect to Flow 4 (Account & Subscription)
{
  "message": "Type 'upgrade' to see all our plans and pricing!",
  "next_node_id": "end_chat"
}
```

### Shared Topics

Multiple flows may cover the same topic from different angles:

**Upgrade/Pricing:**
- Flow 1: Mentions upgrade path at end of onboarding
- Flow 4: Dedicated upgrade flow with full plan details
- Flow 5: Redirects to upgrade flow when needed

**CORS Issues:**
- Flow 1: Brief CORS fix in onboarding widget setup
- Flow 3: Deep CORS troubleshooting flow
- Flow 5: Redirects to widget flow for CORS errors

**Knowledge Base:**
- Flow 1: Basic upload instructions in onboarding
- Flow 2: Comprehensive upload troubleshooting
- Flow 5: Redirects to knowledge flow for upload issues

---

## Position Coordinates

Nodes have `position_x` and `position_y` for visual flow diagram rendering:

### Layout Strategy

**Horizontal spacing:** 200 pixels between sequential nodes
**Vertical spacing:** 100-200 pixels between parallel branches

**Example layout:**
```
START (100, 100)
  ↓
MESSAGE (300, 100)
  ↓
QUESTION (500, 100)
  ├─ Option 1 → (700, 0)
  ├─ Option 2 → (700, 100)
  ├─ Option 3 → (700, 200)
  └─ Option 4 → (700, 300)
```

**Branch convergence:**
When multiple paths converge to single node:
```
Branch 1 (1000, 50)  ─┐
Branch 2 (1000, 150) ─┼─→ Convergence (1200, 100)
Branch 3 (1000, 250) ─┘
```

---

## Configuration Best Practices

### 1. Message Clarity
- Use emojis sparingly (only for visual hierarchy)
- Keep messages concise (2-4 sentences max)
- Use numbered lists for instructions
- Use bullet points for options/features

### 2. Question Design
- Ask one thing at a time
- Provide 2-4 options (max 5)
- Make options mutually exclusive
- Order options logically (most common first)

### 3. RAG Query Optimization
- Use natural language queries
- Include context keywords
- Always provide fallback
- Test with actual knowledge base

### 4. Text Input
- Set clear expectations in prompt
- Provide helpful placeholder examples
- Always handle input with escalation or processing

### 5. Escalation
- Use descriptive subjects (for ticket systems)
- Explain reason (helps support team)
- Collect relevant info before escalating

### 6. End Nodes
- Thank the user
- Offer next steps when relevant
- Set offer_restart appropriately

---

## Example: Complete Node Path

Here's a complete path through Flow 1 (Getting Started):

```
1. START
   config: {"next_node_id": "welcome_msg"}

2. MESSAGE (welcome_msg)
   config: {
     "message": "👋 Welcome to Luran!...",
     "next_node_id": "setup_stage_q"
   }

3. QUESTION (setup_stage_q)
   config: {
     "question": "Where are you in the setup process?",
     "options": [
       {"text": "Just signed up", "next_node_id": "account_setup_branch"},
       ...
     ]
   }

4. MESSAGE (account_setup_branch)
   config: {
     "message": "Great! Let's verify your account...",
     "next_node_id": "email_verification_q"
   }

5. QUESTION (email_verification_q)
   config: {
     "question": "Have you verified your email?",
     "options": [
       {"text": "Yes", "next_node_id": "knowledge_upload_guide"},
       ...
     ]
   }

6. MESSAGE (knowledge_upload_guide)
   config: {
     "message": "✅ Perfect! Now let's add your knowledge base...",
     "next_node_id": "content_type_q"
   }

7. QUESTION (content_type_q)
   config: {
     "question": "What type of content do you want to upload?",
     "options": [
       {"text": "Documents", "next_node_id": "document_upload_guide"},
       ...
     ]
   }

8. MESSAGE (document_upload_guide)
   config: {
     "message": "📄 To upload documents:\n1. Go to Dashboard...",
     "next_node_id": "upload_success_q"
   }

9. QUESTION (upload_success_q)
   config: {
     "question": "How did the upload go?",
     "options": [
       {"text": "Success!", "next_node_id": "widget_setup_branch"},
       ...
     ]
   }

10. MESSAGE (widget_setup_branch)
    config: {
      "message": "🎨 Great! Now let's integrate the widget...",
      "next_node_id": "platform_q"
    }

... continues through widget setup → testing → completion
```

---

## Testing Node Configurations

### Verify Node Structure

```sql
-- Check node has all required config keys
SELECT
  id,
  node_type,
  config ? 'message' as has_message,
  config ? 'next_node_id' as has_next,
  config ? 'question' as has_question,
  config ? 'options' as has_options
FROM flow_nodes
WHERE flow_id = 'your_flow_id';
```

### Validate Next Node References

```sql
-- Find broken next_node_id references
SELECT
  fn1.id as node_id,
  fn1.config->>'next_node_id' as references,
  fn2.id as target_exists
FROM flow_nodes fn1
LEFT JOIN flow_nodes fn2 ON fn1.config->>'next_node_id' = fn2.id::text
WHERE fn1.flow_id = 'your_flow_id'
  AND fn1.config->>'next_node_id' IS NOT NULL
  AND fn2.id IS NULL;
```

### Check Flow Completeness

```sql
-- Ensure every flow has START and at least one END
SELECT
  f.id,
  f.name,
  SUM(CASE WHEN fn.node_type = 'START' THEN 1 ELSE 0 END) as start_count,
  SUM(CASE WHEN fn.node_type = 'END' THEN 1 ELSE 0 END) as end_count
FROM flows f
JOIN flow_nodes fn ON f.id = fn.flow_id
GROUP BY f.id, f.name
HAVING SUM(CASE WHEN fn.node_type = 'START' THEN 1 ELSE 0 END) != 1
   OR SUM(CASE WHEN fn.node_type = 'END' THEN 1 ELSE 0 END) = 0;
```

---

## Appendix: Full Node Type Schema

```typescript
type NodeConfig =
  | StartConfig
  | MessageConfig
  | QuestionConfig
  | RAGQueryConfig
  | TextInputConfig
  | EscalateConfig
  | EndConfig;

interface StartConfig {
  next_node_id: string;
}

interface MessageConfig {
  message: string;
  next_node_id: string;
}

interface QuestionConfig {
  question: string;
  options: Array<{
    text: string;
    next_node_id: string;
  }>;
}

interface RAGQueryConfig {
  query_template: string;
  next_node_id: string;
  fallback_node_id: string;
}

interface TextInputConfig {
  prompt: string;
  placeholder: string;
  next_node_id: string;
}

interface EscalateConfig {
  subject: string;
  reason: string;
  end_flow: boolean;
}

interface EndConfig {
  message: string;
  offer_restart: boolean;
}
```

---

*Reference guide for Luran Support Flows implementation*
*Last updated: 2026-02-06*
