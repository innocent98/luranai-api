# Luran Subscription Plans

**Last Updated:** 2026-02-04
**AI Model:** GPT-4o-mini ($0.150/1M input, $0.600/1M output)
**Exchange Rate:** $1 = ₦1,500

---

## Plan Overview

| Tier | Monthly Price | Annual Price | Conversations/Month | Price per Conversation |
|------|--------------|--------------|---------------------|------------------------|
| **Trial** | ₦0 | ₦0 | 200 | ₦0 (Free) |
| **Lite** | ₦9,000 | ₦90,000 | 2,000 | ₦4.50 |
| **Professional** | ₦40,000 | ₦400,000 | 10,000 | ₦4.00 |
| **Growth** | ₦80,000 | ₦800,000 | 25,000 | ₦3.20 |
| **Enterprise** | Custom | Custom | Unlimited | Custom |

---

## Cost Analysis per Conversation

| Tier | Avg. Input Tokens | Avg. Output Tokens | AI Cost/Convo | Revenue/Convo | Profit/Convo | Margin |
|------|-------------------|--------------------|--------------:|---------------:|--------------:|-------:|
| **Trial** | 1,000 | 560 | ₦0.74 | ₦0.00 | -₦0.74 | -100% |
| **Lite** | 1,000 | 700 | ₦0.86 | ₦4.50 | ₦3.64 | 81% |
| **Professional** | 1,000 | 1,050 | ₦1.17 | ₦4.00 | ₦2.83 | 71% |
| **Growth** | 1,000 | 1,050 | ₦1.17 | ₦3.20 | ₦2.03 | 63% |
| **Enterprise** | 1,000 | 1,400 | ₦1.49 | Custom | Custom | ~60-70% |

**Notes:**
- Input tokens = user query (~100) + retrieved context (~700) + system prompt (~200)
- Output tokens = 70% of max_response_tokens (realistic average usage)
- Costs exclude infrastructure (~₦2,000-4,000/tenant/month)
- Embedding costs negligible ($0.00002/1K tokens)

---

## Monthly Profit Analysis

| Tier | Conversations | Total AI Cost | Total Revenue | Gross Profit | Net Margin |
|------|--------------|---------------|---------------|--------------|------------|
| **Trial** | 200 | ₦148 | ₦0 | -₦148 | Loss (acquisition) |
| **Lite** | 2,000 | ₦1,720 | ₦9,000 | ₦7,280 | 81% |
| **Professional** | 10,000 | ₦11,700 | ₦40,000 | ₦28,300 | 71% |
| **Growth** | 25,000 | ₦29,250 | ₦80,000 | ₦50,750 | 63% |
| **Enterprise** | 50,000+ | ₦74,500+ | Custom | Custom | 60-70% |

**After infrastructure costs (₦3,000/month):**
- **Lite:** ₦4,280 net profit (48% margin)
- **Professional:** ₦25,300 net profit (63% margin)
- **Growth:** ₦47,750 net profit (60% margin)

---

## Trial Tier

**Price:** Free
**Duration:** 30 days
**Conversations:** 200/month

### Features
- 2 knowledge sources
- 10MB storage
- 200 conversations/month
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

**Price:** ₦9,000/month or ₦90,000/year (save ₦18,000)
**Conversations:** 2,000/month

### Features
- 15 knowledge sources
- 50MB storage
- 2,000 conversations/month
- 3 team members
- 2 automation flows
- URL scraping
- Analytics dashboard
- Priority email support
- Max response tokens: 1,000

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
**Conversations:** 10,000/month

### Features
- 50 knowledge sources
- 200MB storage
- 10,000 conversations/month
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
**Conversations:** 25,000/month

### Features
- 50 knowledge sources
- 500MB storage
- 25,000 conversations/month
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
- **Increase:** 10x conversations (200 → 2,000), 7.5x knowledge sources

### From Lite → Professional
- **Unlock:** Webhooks (3/flow), advanced AI responses
- **Increase:** 5x conversations, 5x flows, 2.5x nodes/flow

### From Professional → Growth
- **Unlock:** Advanced webhooks (10/flow), custom integrations
- **Increase:** 2.5x conversations, 2.5x flows, 2x nodes/flow

### From Growth → Enterprise
- **Unlock:** Unlimited everything, dedicated support, SLA
- **Custom:** Volume-based pricing, white-label options
