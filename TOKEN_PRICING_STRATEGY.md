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


## Keep this here for reference
 My honest take: At these profit margins (87-94%), quality matters more than cost. If GPT-4o-mini reduces escalations by even 5%, the saved support costs will exceed the extra ₦95K/month AI cost.
                                                                                                                                                                                                    
  Would you like me to:                                                                                                                                                                             
  1. Create the Enterprise pricing calculation function?                                                                                                                                            
  2. Add volume-based pricing tiers to your database?                                                                                                                                               
  3. Build a cost calculator for different AI models?  


  ### second
   Action Items                                                                                                                                                                                      
                                                                                                                                                                                                    
  1. Track actual output tokens - Add logging to see real averages:                                                                                                                                 
  # In rag_service.py, line 750-752                                                                                                                                                                 
  candidates_token_count = response.usage_metadata.candidates_token_count                                                                                                                           
  prompt_token_count = response.usage_metadata.prompt_token_count                                                                                                                                   
  logger.info(f"Input: {prompt_token_count}, Output: {candidates_token_count}")                                                                                                                     
                                                                                                                                                                                                    
  2. Monitor for 1-2 weeks to get actual data                                                                                                                                                       
  3. Calculate real costs based on actual usage                                                                                                                                                     
                                                                                                                                                                                                    
  Would you like me to add the token tracking code so you can see the real output token averages? 


  ### CURRENT PRICING ANALYSIS (Facts)                                                                                                                                                                  
                                                                                                                                                                                                    
  Per-Conversation Cost to Customer                                                                                                                                                                 
  ┌──────────────┬───────────────┬───────────────┬───────────────────┬──────────────────┬──────────────────┐                                                                                        
  │     Tier     │ Monthly Price │ Conversations │ Cost/Conversation │ AI Cost (Gemini) │ Your Profit/Conv │                                                                                        
  ├──────────────┼───────────────┼───────────────┼───────────────────┼──────────────────┼──────────────────┤                                                                                        
  │ Trial        │ ₦0            │ 100           │ ₦0                │ ₦0.56            │ -₦0.56 (loss)    │                                                                                        
  ├──────────────┼───────────────┼───────────────┼───────────────────┼──────────────────┼──────────────────┤                                                                                        
  │ Lite         │ ₦12,000       │ 1,000         │ ₦12.00            │ ₦0.56            │ ₦11.44           │                                                                                        
  ├──────────────┼───────────────┼───────────────┼───────────────────┼──────────────────┼──────────────────┤                                                                                        
  │ Professional │ ₦35,000       │ 5,000         │ ₦7.00             │ ₦0.56            │ ₦6.44            │                                                                                        
  ├──────────────┼───────────────┼───────────────┼───────────────────┼──────────────────┼──────────────────┤                                                                                        
  │ Enterprise   │ Custom        │ Unlimited     │ ???               │ ₦0.56-0.66       │ ???              │                                                                                        
  └──────────────┴───────────────┴───────────────┴───────────────────┴──────────────────┴──────────────────┘                                                                                        
  ---                                                                                                                                                                                               
  HONEST ASSESSMENT: Is This Good or One-Sided?                                                                                                                                                     
                                                                                                                                                                                                    
  🚨 LITE TIER IS OVERPRICED (One-sided toward you)                                                                                                                                                 
                                                                                                                                                                                                    
  Customer pays: ₦12 per conversation                                                                                                                                                               
  Your cost: ₦0.56 per conversation                                                                                                                                                                 
  Markup: 21x (2,100%)                                                                                                                                                                              
                                                                                                                                                                                                    
  Problems:                                                                                                                                                                                         
  1. ₦12/conversation is expensive for small businesses                                                                                                                                             
  2. Poor value proposition - customer might feel ripped off                                                                                                                                        
  3. Competitor risk - Others could undercut you at ₦5-8/conversation                                                                                                                               
  4. Low usage risk - Customer might pay ₦12,000 but only use 300 conversations = ₦40/conversation                                                                                                  
                                                                                                                                                                                                    
  Who this hurts:                                                                                                                                                                                   
  - Small businesses trying out the platform                                                                                                                                                        
  - Low-volume users who want basic features                                                                                                                                                        
  - Price-sensitive markets                                                                                                                                                                         
                                                                                                                                                                                                    
  ✅ PROFESSIONAL TIER IS FAIRLY PRICED (Balanced)                                                                                                                                                  
                                                                                                                                                                                                    
  Customer pays: ₦7 per conversation                                                                                                                                                                
  Your cost: ₦0.56 per conversation                                                                                                                                                                 
  Markup: 12.5x (1,250%)                                                                                                                                                                            
                                                                                                                                                                                                    
  Why this works:                                                                                                                                                                                   
  1. 42% discount from Lite - clear upgrade incentive                                                                                                                                               
  2. ₦7/conversation feels reasonable for SMBs                                                                                                                                                      
  3. 5,000 conversations/month = good volume for growing businesses                                                                                                                                 
  4. Still 91% margin for you - very profitable                                                                                                                                                     
                                                                                                                                                                                                    
  Sweet spot pricing.                                                                                                                                                                               
                                                                                                                                                                                                    
  ⚠️ CONVERSATION LIMITS ARE TOO LOW (Hurts customers)                                                                                                                                              
                                                                                                                                                                                                    
  Reality check:                                                                                                                                                                                    
  ┌───────────────────┬────────────────┬──────────────┬────────────┬───────────────┐                                                                                                                
  │ Conversations/Day │ Monthly Volume │ Current Tier │   Price    │  Realistic?   │                                                                                                                
  ├───────────────────┼────────────────┼──────────────┼────────────┼───────────────┤                                                                                                                
  │ 10/day            │ 300            │ Trial (100)  │ ❌ Blocked │ Too low       │                                                                                                                
  ├───────────────────┼────────────────┼──────────────┼────────────┼───────────────┤                                                                                                                
  │ 33/day            │ 1,000          │ Lite         │ ₦12,000    │ Barely enough │                                                                                                                
  ├───────────────────┼────────────────┼──────────────┼────────────┼───────────────┤                                                                                                                
  │ 166/day           │ 5,000          │ Professional │ ₦35,000    │ OK for SMB    │                                                                                                                
  ├───────────────────┼────────────────┼──────────────┼────────────┼───────────────┤                                                                                                                
  │ 333/day           │ 10,000         │ ???          │ ???        │ GAP           │                                                                                                                
  ├───────────────────┼────────────────┼──────────────┼────────────┼───────────────┤                                                                                                                
  │ 1,666/day         │ 50,000         │ ???          │ ???        │ GAP           │                                                                                                                
  ├───────────────────┼────────────────┼──────────────┼────────────┼───────────────┤                                                                                                                
  │ 3,333/day         │ 100,000        │ Enterprise   │ Custom     │ Finally       │                                                                                                                
  └───────────────────┴────────────────┴──────────────┴────────────┴───────────────┘                                                                                                                
  The Problem:                                                                                                                                                                                      
  - Huge gap between Professional (5K) and Enterprise (unlimited)                                                                                                                                   
  - Customers doing 10K-50K conversations/month have NO tier                                                                                                                                        
  - They're forced to jump to Enterprise pricing                                                                                                                                                    
                                                                                                                                                                                                    
  Missing tier:                                                                                                                                                                                     
  - Growth/Scale tier: 10K-25K conversations at ₦50K-₦80K/month                                                                                                                                     
  - Fills the gap between SMB and Enterprise                                                                                                                                                        
                                                                                                                                                                                                    
  ---                                                                                                                                                                                               
  YOUR PROPOSED ENTERPRISE DISCOUNT: 20% OFF                                                                                                                                                        
                                                                                                                                                                                                    
  Option 1: 20% off Lite pricing                                                                                                                                                                    
                                                                                                                                                                                                    
  Lite: ₦12/conversation                                                                                                                                                                            
  Enterprise (20% off): ₦9.60/conversation                                                                                                                                                          
  ┌─────────┬────────────┬──────────────────┬────────────┬────────┐                                                                                                                                 
  │ Volume  │    Cost    │ AI Cost (Gemini) │   Profit   │ Margin │                                                                                                                                 
  ├─────────┼────────────┼──────────────────┼────────────┼────────┤                                                                                                                                 
  │ 50,000  │ ₦480,000   │ ₦28,120          │ ₦451,880   │ 94.1%  │                                                                                                                                 
  ├─────────┼────────────┼──────────────────┼────────────┼────────┤                                                                                                                                 
  │ 100,000 │ ₦960,000   │ ₦56,240          │ ₦903,760   │ 94.1%  │                                                                                                                                 
  ├─────────┼────────────┼──────────────────┼────────────┼────────┤                                                                                                                                 
  │ 300,000 │ ₦2,880,000 │ ₦168,720         │ ₦2,711,280 │ 94.1%  │                                                                                                                                 
  └─────────┴────────────┴──────────────────┴────────────┴────────┘                                                                                                                                 
  My honest opinion: ❌ TOO EXPENSIVE                                                                                                                                                               
  - ₦2.88M for 300K conversations is NOT competitive                                                                                                                                                
  - Much higher than your earlier ₦1.5M-₦1.8M proposal                                                                                                                                              
  - Large customers will negotiate hard or walk away                                                                                                                                                
                                                                                                                                                                                                    
  Option 2: 20% off Professional pricing ✅                                                                                                                                                         
                                                                                                                                                                                                    
  Professional: ₦7/conversation                                                                                                                                                                     
  Enterprise (20% off): ₦5.60/conversation                                                                                                                                                          
  ┌─────────┬────────────┬──────────────────┬────────────┬────────┐                                                                                                                                 
  │ Volume  │    Cost    │ AI Cost (Gemini) │   Profit   │ Margin │                                                                                                                                 
  ├─────────┼────────────┼──────────────────┼────────────┼────────┤                                                                                                                                 
  │ 50,000  │ ₦280,000   │ ₦28,120          │ ₦251,880   │ 90.0%  │                                                                                                                                 
  ├─────────┼────────────┼──────────────────┼────────────┼────────┤                                                                                                                                 
  │ 100,000 │ ₦560,000   │ ₦56,240          │ ₦503,760   │ 90.0%  │                                                                                                                                 
  ├─────────┼────────────┼──────────────────┼────────────┼────────┤                                                                                                                                 
  │ 300,000 │ ₦1,680,000 │ ₦168,720         │ ₦1,511,280 │ 90.0%  │                                                                                                                                 
  └─────────┴────────────┴──────────────────┴────────────┴────────┘                                                                                                                                 
  My honest opinion: ✅ REASONABLE                                                                                                                                                                  
  - ₦1.68M for 300K conversations matches your ₦1.5M-₦1.8M target                                                                                                                                   
  - 90% margin is still excellent                                                                                                                                                                   
  - ₦5.60/conversation is competitive for Enterprise                                                                                                                                                
  - Fair discount for bulk volume                                                                                                                                                                   
                                                                                                                                                                                                    
  Option 3: Tiered Volume Discount (Better)                                                                                                                                                         
                                                                                                                                                                                                    
  Instead of flat 20%, use volume-based pricing:                                                                                                                                                    
  ┌────────────────┬────────────────────┬───────────────────┬──────────────┐                                                                                                                        
  │ Monthly Volume │ Price/Conversation │ Discount from Pro │ Monthly Cost │                                                                                                                        
  ├────────────────┼────────────────────┼───────────────────┼──────────────┤                                                                                                                        
  │ 5,000 (Pro)    │ ₦7.00              │ 0% (base)         │ ₦35,000      │                                                                                                                        
  ├────────────────┼────────────────────┼───────────────────┼──────────────┤                                                                                                                        
  │ 10,000-25,000  │ ₦6.00              │ 14% off           │ ₦60K-₦150K   │                                                                                                                        
  ├────────────────┼────────────────────┼───────────────────┼──────────────┤                                                                                                                        
  │ 25,000-75,000  │ ₦5.00              │ 29% off           │ ₦125K-₦375K  │                                                                                                                        
  ├────────────────┼────────────────────┼───────────────────┼──────────────┤                                                                                                                        
  │ 75,000-150,000 │ ₦4.00              │ 43% off           │ ₦300K-₦600K  │                                                                                                                        
  ├────────────────┼────────────────────┼───────────────────┼──────────────┤                                                                                                                        
  │ 150,000+       │ ₦3.50              │ 50% off           │ ₦525K+       │                                                                                                                        
  └────────────────┴────────────────────┴───────────────────┴──────────────┘                                                                                                                        
  At 300,000 conversations: ₦3.50/conv = ₦1,050,000/month                                                                                                                                           
  - AI Cost: ₦168,720                                                                                                                                                                               
  - Profit: ₦881,280                                                                                                                                                                                
  - Margin: 84% (still excellent)                                                                                                                                                                   
                                                                                                                                                                                                    
  Why this is better:                                                                                                                                                                               
  1. Rewards higher volume - the more they use, the cheaper it gets                                                                                                                                 
  2. Sticky pricing - hard to churn when they're at ₦3.50/conversation                                                                                                                              
  3. Competitive - matches SaaS pricing models                                                                                                                                                      
  4. Fair for both sides - customer saves, you still profit 84%                                                                                                                                     
                                                                                                                                                                                                    
  ---                                                                                                                                                                                               
  RECOMMENDED PRICING RESTRUCTURE                                                                                                                                                                   
                                                                                                                                                                                                    
  Current vs Proposed                                                                                                                                                                               
  ┌──────────────┬───────────────┬───────────────┬──────────────┬───────────┬────────────┐                                                                                                          
  │     Tier     │ Current Price │ Current Convs │  New Price   │ New Convs │ Cost/Conv  │                                                                                                          
  ├──────────────┼───────────────┼───────────────┼──────────────┼───────────┼────────────┤                                                                                                          
  │ Trial        │ ₦0            │ 100           │ ₦0           │ 200       │ ₦0         │                                                                                                          
  ├──────────────┼───────────────┼───────────────┼──────────────┼───────────┼────────────┤                                                                                                          
  │ Lite         │ ₦12,000       │ 1,000         │ ₦9,000       │ 2,000     │ ₦4.50      │                                                                                                          
  ├──────────────┼───────────────┼───────────────┼──────────────┼───────────┼────────────┤                                                                                                          
  │ Professional │ ₦35,000       │ 5,000         │ ₦35,000      │ 10,000    │ ₦3.50      │                                                                                                          
  ├──────────────┼───────────────┼───────────────┼──────────────┼───────────┼────────────┤                                                                                                          
  │ Growth (NEW) │ -             │ -             │ ₦75,000      │ 25,000    │ ₦3.00      │                                                                                                          
  ├──────────────┼───────────────┼───────────────┼──────────────┼───────────┼────────────┤                                                                                                          
  │ Enterprise   │ Custom        │ Unlimited     │ Volume-based │ Unlimited │ ₦2.50-4.00 │                                                                                                          
  └──────────────┴───────────────┴───────────────┴──────────────┴───────────┴────────────┘                                                                                                          
  Why These Changes?                                                                                                                                                                                
                                                                                                                                                                                                    
  Trial (200 convs):                                                                                                                                                                                
  - Current 100 is too limiting (3.3/day)                                                                                                                                                           
  - 200 gives 6-7 conversations/day                                                                                                                                                                 
  - Still low enough to push upgrades                                                                                                                                                               
  - Small cost increase for you (₦56 → ₦112)                                                                                                                                                        
                                                                                                                                                                                                    
  Lite (₦9K for 2,000 convs):                                                                                                                                                                       
  - 25% price reduction but 2x conversations                                                                                                                                                        
  - ₦4.50/conversation (down from ₦12) = much better value                                                                                                                                          
  - 2,000 conversations = 66/day (realistic for small business)                                                                                                                                     
  - Your margin: 88% (down from 95%, still excellent)                                                                                                                                               
  - More competitive, less sticker shock                                                                                                                                                            
                                                                                                                                                                                                    
  Professional (₦35K for 10,000 convs):                                                                                                                                                             
  - Same price but 2x conversations                                                                                                                                                                 
  - ₦3.50/conversation (down from ₦7)                                                                                                                                                               
  - 10,000 conversations = 333/day (good for growing SMB)                                                                                                                                           
  - Your margin: 84% (still very healthy)                                                                                                                                                           
                                                                                                                                                                                                    
  Growth (NEW - ₦75K for 25,000 convs):                                                                                                                                                             
  - Fills the gap between Pro and Enterprise                                                                                                                                                        
  - ₦3.00/conversation                                                                                                                                                                              
  - 25,000 conversations = 833/day                                                                                                                                                                  
  - Targets mid-market customers                                                                                                                                                                    
  - Your margin: 81%                                                                                                                                                                                
                                                                                                                                                                                                    
  Enterprise (Volume-based):                                                                                                                                                                        
  - 20% off Professional = ₦5.60/conv base                                                                                                                                                          
  - Volume discounts down to ₦2.50-3.50/conv at scale                                                                                                                                               
  - Custom pricing for 100K+ volumes                                                                                                                                                                
  - Your margin: 75-85%                                                                                                                                                                             
                                                                                                                                                                                                    
  ---                                                                                                                                                                                               
  PROFIT COMPARISON (300K Conversations)                                                                                                                                                            
                                                                                                                                                                                                    
  Current Structure                                                                                                                                                                                 
                                                                                                                                                                                                    
  No clear tier - forced to negotiate custom Enterprise                                                                                                                                             
                                                                                                                                                                                                    
  Your Proposal: 20% off Professional                                                                                                                                                               
                                                                                                                                                                                                    
  - Price: ₦5.60/conv = ₦1,680,000                                                                                                                                                                  
  - Profit: ₦1,511,280                                                                                                                                                                              
  - Margin: 90%                                                                                                                                                                                     
  - Assessment: ✅ Fair                                                                                                                                                                             
                                                                                                                                                                                                    
  My Proposal: Volume-based                                                                                                                                                                         
                                                                                                                                                                                                    
  - Price: ₦3.50/conv = ₦1,050,000                                                                                                                                                                  
  - Profit: ₦881,280                                                                                                                                                                                
  - Margin: 84%                                                                                                                                                                                     
  - Assessment: ✅ Better for customer acquisition, still profitable   

  #### Question
  is it possible to reduce the input token and maybe compress, mostly the system prompt? or we have a balance prompt already?
