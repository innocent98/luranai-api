# Luran Subscription Plans

**Last Updated:** 2026-10-29 (monthly conversation limits lowered to Trial 100 / Lite 1,000 / Professional 5,000 / Growth 10,000, see `docs/sop/conversation_limit_numbers.md`; earlier: Lite price and plan gates; the cost analysis below is from 2026-02 and uses the old Lite price in its per-conversation figures where not restated)
**AI Model:** GPT-4o-mini ($0.150/1M input, $0.600/1M output)
**Exchange Rate:** $1 = ₦1,500

---

## Plan Overview

| Tier | Monthly Price | Annual Price | Conversations/Month | Price per Conversation |
|------|--------------|--------------|---------------------|------------------------|
| **Trial** | ₦0 | ₦0 | 100 | ₦0 (Free) |
| **Lite** | ₦15,000 | ₦150,000 | 1,000 | ₦15.00 |
| **Professional** | ₦40,000 | ₦400,000 | 5,000 | ₦8.00 |
| **Growth** | ₦80,000 | ₦800,000 | 10,000 | ₦8.00 |
| **Enterprise** | Custom | Custom | Unlimited | Custom |

**What counts as a conversation?** A conversation is one continuous exchange between a customer and your AI or team, from their first message until they have been quiet for 6 hours. It is not counted per message: one conversation can hold any number of messages, images and replies. If the same customer comes back within 6 hours it is still the same conversation; after 6 hours it is a new one. A conversation handed to a human agent stays one conversation until it is resolved. Conversations are counted per calendar month (UTC) and the count resets on the 1st. WhatsApp follows the same rule per chat.

---

## Cost Analysis per Conversation

| Tier | Avg. Input Tokens | Avg. Output Tokens | AI Cost/Convo | Revenue/Convo | Profit/Convo | Margin |
|------|-------------------|--------------------|--------------:|---------------:|--------------:|-------:|
| **Trial** | 1,000 | 560 | ₦0.74 | ₦0.00 | -₦0.74 | -100% |
| **Lite** | 1,000 | 700 | ₦0.86 | ₦15.00 | ₦14.14 | 94% |
| **Professional** | 1,000 | 1,050 | ₦1.17 | ₦8.00 | ₦6.83 | 85% |
| **Growth** | 1,000 | 1,050 | ₦1.17 | ₦8.00 | ₦6.83 | 85% |
| **Enterprise** | 1,000 | 1,400 | ₦1.49 | Custom | Custom | ~60-70% |

**Notes:**
- 2026-10-29: the cost columns are the original gpt-4o-mini estimate. On gpt-6-luna a conversation is about 7,050 tokens ($0.10/M in, $0.50/M out), under ₦6 even if every token were output, so cost is not what sets the limits; the limits are packaging.
- Input tokens = user query (~100) + retrieved context (~700) + system prompt (~200)
- Output tokens = 70% of max_response_tokens (realistic average usage)
- Costs exclude infrastructure (~₦2,000-4,000/tenant/month)
- Embedding costs negligible ($0.00002/1K tokens)

---

## Monthly Profit Analysis

| Tier | Conversations | Total AI Cost | Total Revenue | Gross Profit | Net Margin |
|------|--------------|---------------|---------------|--------------|------------|
| **Trial** | 100 | ₦74 | ₦0 | -₦74 | Loss (acquisition) |
| **Lite** | 1,000 | ₦860 | ₦15,000 | ₦14,140 | 94% |
| **Professional** | 5,000 | ₦5,850 | ₦40,000 | ₦34,150 | 85% |
| **Growth** | 10,000 | ₦11,700 | ₦80,000 | ₦68,300 | 85% |
| **Enterprise** | 50,000+ | ₦74,500+ | Custom | Custom | 60-70% |

**After infrastructure costs (₦3,000/month):**
- **Lite:** ₦11,140 net profit (74% margin)
- **Professional:** ₦31,150 net profit (78% margin)
- **Growth:** ₦65,300 net profit (82% margin)

---

## Trial Tier

**Price:** Free
**Duration:** 30 days
**Conversations:** 100/month

### Features
- 2 knowledge sources
- 10MB storage
- 100 conversations/month
- 1 team member
- Email support
- Max response tokens: 800

### Capabilities
| Capability | Limit |
|------------|-------|
| Analytics Dashboard | ❌ No |
| URL Scraping | ❌ No |
| Automation Flows | 0 |
| Webhook Integrations | ❌ No |

---

## Lite Tier

**Price:** ₦15,000/month or ₦150,000/year (10 months of monthly; changed from ₦9,000 on 2026-10-28, new signups only)
**Conversations:** 1,000/month

### Features
- 20 knowledge sources
- 50MB storage
- 1,000 conversations/month
- 3 team members
- 3 flows
- 2 AI actions
- URL scraping
- Analytics dashboard
- WhatsApp (beta): 1 number
- Not included: meeting booking and quotes, Slack and CRM notifications, screen recording, ratings breakdown and CSV export, lookup chains, webhooks (these start at Professional)
- Max response tokens: 800

### Capabilities
| Capability | Limit |
|------------|-------|
| Analytics Dashboard | ✅ Yes |
| URL Scraping | ✅ Yes |
| Automation Flows | 2 flows, 20 nodes/flow |
| Webhook Integrations | ❌ No |

---

## Professional Tier

**Price:** ₦40,000/month or ₦400,000/year (save ₦80,000)
**Conversations:** 5,000/month

### Features
- 50 knowledge sources
- 200MB storage
- 5,000 conversations/month
- 10 team members
- 10 automation flows
- Webhook integrations (3/flow)
- URL scraping
- Analytics dashboard
- Advanced AI responses
- Priority support
- Max response tokens: 1,500

### Capabilities
| Capability | Limit |
|------------|-------|
| Analytics Dashboard | ✅ Yes |
| URL Scraping | ✅ Yes |
| Automation Flows | 10 flows, 50 nodes/flow |
| Webhook Integrations | ✅ Yes (3 webhooks/flow) |

---

## Growth Tier

**Price:** ₦80,000/month or ₦800,000/year (save ₦160,000)
**Conversations:** 10,000/month

### Features
- 50 knowledge sources
- 500MB storage
- 10,000 conversations/month
- 20 team members
- 25 automation flows
- Advanced webhook integrations (10/flow)
- URL scraping
- Advanced analytics
- Priority support
- Custom integrations
- Max response tokens: 1,500

### Capabilities
| Capability | Limit |
|------------|-------|
| Analytics Dashboard | ✅ Yes |
| URL Scraping | ✅ Yes |
| Automation Flows | 25 flows, 100 nodes/flow |
| Webhook Integrations | ✅ Yes (10 webhooks/flow) |

---

## Enterprise Tier

**Price:** Custom (contact sales)
**Conversations:** Unlimited (volume-based pricing)

### Features
- Unlimited knowledge sources
- Unlimited storage
- Volume-based pricing (25K-300K+ conversations)
- Unlimited team members
- Unlimited automation flows
- Unlimited webhooks
- Dedicated account manager
- Custom integrations
- SLA guarantees
- 24/7 premium support
- Max response tokens: 2,000

### Capabilities
| Capability | Limit |
|------------|-------|
| Analytics Dashboard | ✅ Yes |
| URL Scraping | ✅ Yes |
| Automation Flows | ♾️ Unlimited |
| Webhook Integrations | ♾️ Unlimited |

---

## Plan gates (entitlements, 2026-10-28)

The single definition of every plan row is `app/services/plan_catalog.py`; the `features` strings on
`GET /subscription/plans` are generated from it. What each tier includes:

| Capability | Trial | Lite | Professional | Growth | Enterprise |
|---|---|---|---|---|---|
| Meeting booking and quotes (`sales_close`) | yes | no | yes | yes | yes |
| Slack and CRM notifications (`crm_notifications`) | yes | no | yes | yes | yes |
| Screen recording | yes | no | yes | yes | yes |
| Ratings breakdown and CSV export | yes | no | yes | yes | yes |
| AI actions (max) | 2 | 2 | 15 | 40 | unlimited |
| Lookup chains and webhook nodes | no | no | yes | yes | yes |
| Analytics | no | yes | yes | yes | yes |
| WhatsApp numbers (every plan includes WhatsApp since 2026-10-29; no grant) | 1 | 1 | 2 | 5 | unlimited |
| Voice | platform grant only, never from a plan | | | | |

Never gated: human handoff, tickets, the agent app, image upload, rating collection, lead capture,
scoring and the lead email notice, the web widget. Audits, conversations, seats, sources, storage and
flows keep their per-plan limits. Details: `docs/sop/plan_entitlements.md`.

## Technical Details

### AI Configuration
- **LLM Model:** GPT-4o-mini
- **Embedding Model:** text-embedding-3-small (1536 dimensions)
- **Rate Limits:** 500 RPM (requests per minute)
- **Context Window:** 128K tokens

### Response Token Limits by Tier
| Tier | Max Response Tokens | Typical Response Length |
|------|--------------------:|------------------------:|
| Trial | 800 | ~600 words |
| Lite | 1,000 | ~750 words |
| Professional | 1,500 | ~1,125 words |
| Growth | 1,500 | ~1,125 words |
| Enterprise | 2,000 | ~1,500 words |

### Flow Automation Limits
| Tier | Max Flows | Nodes/Flow | Webhooks/Flow | Triggers |
|------|-----------|------------|---------------|----------|
| Trial | 0 | 0 | 0 | N/A |
| Lite | 2 | 20 | 0 | Keyword, First Message |
| Professional | 10 | 50 | 3 | All types |
| Growth | 25 | 100 | 10 | All types |
| Enterprise | ♾️ | ♾️ | ♾️ | All types |

**Trigger Types:** Manual, Keyword, Intent Detection, Schedule, Event

---

## Upgrade Paths

### From Trial → Lite
- **Unlock:** Analytics, URL scraping, 2 automation flows
- **Increase:** 10x conversations (100 → 1,000), 7.5x knowledge sources

### From Lite → Professional
- **Unlock:** Webhooks (3/flow), advanced AI responses
- **Increase:** 5x conversations, 5x flows, 2.5x nodes/flow

### From Professional → Growth
- **Unlock:** Advanced webhooks (10/flow), custom integrations
- **Increase:** 2x conversations, 2.5x flows, 2x nodes/flow

### From Growth → Enterprise
- **Unlock:** Unlimited everything, dedicated support, SLA
- **Custom:** Volume-based pricing, white-label options
