# Token Limits & Pricing Strategy

## Current Token Configuration

### System-Wide Limits
- **Default**: 800 tokens per response
- **Hard Max**: 4000 tokens per response
- **Recommended Range**: 800-2000 tokens

### Current Model (Development)
- **Model**: Gemma 3 12B IT (`models/gemma-3-12b-it`)
- **Cost**: FREE (open source)
- **Rate Limit**: 30 RPM, 14.4K RPD

### Production Model (Ready to Deploy)
- **Model**: Gemini 2.0 Flash (`models/gemini-2.0-flash-exp`)
- **Cost**:
  - Input: ~$0.075 per 1M tokens
  - Output: ~$0.30 per 1M tokens
- **Rate Limit**: 10 RPM (free tier), 1M RPD
- **Benefits**: Faster, higher quality, better formatting, multimodal support

---

## Pricing Tier Recommendations

### Option 1: Simple 3-Tier Model

| Tier | Price/Month | max_response_tokens | Monthly Conversations | Estimated Cost |
|------|-------------|---------------------|----------------------|----------------|
| **Free** | $0 | 400 | 100 | ~$0.05 |
| **Starter** | $29 | 800 | 1,000 | ~$0.50 |
| **Pro** | $99 | 1,500 | 5,000 | ~$2.40 |
| **Enterprise** | $299+ | 4,000 | Unlimited | ~$10-50+ |

### Option 2: Usage-Based Model

| Tier | Base Price | max_response_tokens | Included Conversations | Extra Conversations |
|------|------------|---------------------|------------------------|---------------------|
| **Starter** | $19/mo | 800 | 500 | $0.05 per convo |
| **Growth** | $79/mo | 1,200 | 3,000 | $0.03 per convo |
| **Scale** | $199/mo | 2,000 | 10,000 | $0.02 per convo |
| **Enterprise** | Custom | 4,000 | Custom | Custom |

---

## Preventing Token Misuse

### 1. **Hard Limits by Plan**
```python
# In tenant creation/update
pricing_tier_limits = {
    'free': {'max_tokens': 400, 'monthly_convos': 100},
    'starter': {'max_tokens': 800, 'monthly_convos': 1000},
    'pro': {'max_tokens': 1500, 'monthly_convos': 5000},
    'enterprise': {'max_tokens': 4000, 'monthly_convos': None}
}
```

### 2. **Usage Tracking**
- Track tokens used per conversation
- Track total monthly tokens per tenant
- Alert when approaching limits
- Soft throttle at 80% usage
- Hard stop at 100% usage

### 3. **Rate Limiting**
- Per-tenant request limits (e.g., 10 requests/minute)
- Prevents spam/abuse
- Protects API costs

### 4. **Quality Over Quantity**
- Shorter, focused responses are often better
- 800 tokens ≈ 600 words (usually sufficient)
- Reserve high token counts for complex queries

---

## Cost Calculations (Gemini Flash)

### Assumptions
- Average response: 800 tokens
- Input (context + query): ~500 tokens
- Output (AI response): ~800 tokens

### Per Conversation Cost
```
Input:  500 tokens × $0.075/1M = $0.0000375
Output: 800 tokens × $0.30/1M  = $0.00024
Total:                          = $0.0002775 (~$0.00028 per conversation)
```

### Monthly Costs by Volume
- 1,000 conversations: **~$0.28**
- 5,000 conversations: **~$1.39**
- 10,000 conversations: **~$2.78**
- 50,000 conversations: **~$13.88**

### Profit Margins
- **Free Tier** (100 convos/mo): Cost $0.03, Revenue $0 → Loss leader
- **Starter** ($29/mo, 1K convos): Cost $0.28, Revenue $29 → **99% margin**
- **Pro** ($99/mo, 5K convos): Cost $1.39, Revenue $99 → **98.6% margin**
- **Enterprise** ($299/mo, 20K convos): Cost $5.56, Revenue $299 → **98.1% margin**

---

## Implementation Roadmap

### Phase 1: Add Pricing Tiers (Week 1)
- [ ] Add `pricing_tier` field to Tenant model
- [ ] Enforce `max_response_tokens` by tier
- [ ] Add usage tracking table
- [ ] Build billing dashboard

### Phase 2: Usage Monitoring (Week 2)
- [ ] Track tokens per conversation
- [ ] Track monthly token usage
- [ ] Email alerts at 80% usage
- [ ] Soft throttle at 90% usage
- [ ] Hard stop at 100% usage

### Phase 3: Billing Integration (Week 3)
- [ ] Stripe integration
- [ ] Upgrade/downgrade flows
- [ ] Invoice generation
- [ ] Usage-based billing option

### Phase 4: Cost Optimization (Ongoing)
- [ ] Cache common responses
- [ ] Optimize prompt templates (reduce input tokens)
- [ ] A/B test response lengths
- [ ] Consider hybrid approach (Gemma for simple queries, Flash for complex)

---

## Hybrid Model Strategy (Advanced)

### Cost Optimization Idea
```python
if is_simple_query(user_message):
    # Use free Gemma model (saves $0.00028)
    model = "models/gemma-3-12b-it"
else:
    # Use Gemini Flash for complex queries
    model = "models/gemini-2.0-flash-exp"
```

### Potential Savings
- 70% simple queries → Gemma (free)
- 30% complex queries → Flash ($0.00028)
- **Effective cost**: ~$0.000084 per conversation (70% reduction)

---

## Recommendations

1. **Start with Gemma** (current setup) for MVP/beta
2. **Switch to Flash** when you have paying customers (better quality)
3. **Implement tiered pricing** before public launch
4. **Track usage metrics** from day one
5. **Set conservative limits** initially, increase based on demand
6. **Consider hybrid model** at scale (>10K conversations/month)

---

## Notes

- Gemini Flash is **significantly better** at markdown formatting, longer responses, and complex queries
- Gemma is **perfectly fine** for simple Q&A and development
- Token costs are **minimal** compared to potential revenue
- The real cost driver will be **infrastructure** (servers, database, vector DB) not AI tokens
