# Aggressive RAG Configuration (Optional)

## ⚠️ Use This If:
- You have high-quality, comprehensive knowledge base
- Users ask complex, multi-part questions
- Cost is not a primary concern
- You want maximum helpfulness

## 🚨 Don't Use This If:
- Knowledge base is sparse or low-quality
- You're on a tight budget
- Response time is critical
- You need consistent, conservative answers

---

## Configuration Changes

### In `app/services/rag_service.py`:

```python
# Line 27-29: Similarity thresholds
MIN_SCORE_THRESHOLD = 0.35  # Very permissive
MIN_RELEVANT_SIMILARITY = 0.38  # Lower safety gate

# Line 31-33: Token optimization
MAX_CHUNK_CHARS = 1400  # Higher limit

# Line 128: Max sentences
max_sentences: int = 12

# Line 343: Compress call
max_sentences=12,

# Line 674: Re-ranking
top_k=4,  # Keep top 4 chunks

# Line 731: Temperature
'temperature': 0.45,  # More creative

# Line 565 (chat.py): History
max_history_turns=12,

# Line 507 (chat.py): Conversation limit
limit=24  # 12 turns
```

---

## Expected Results

### Good:
- ✅ Nearly all questions get answered
- ✅ Very comprehensive, detailed responses
- ✅ Natural, conversational tone
- ✅ Excellent at complex questions

### Bad:
- ❌ ~30-40% higher token costs
- ❌ Slightly higher hallucination risk (still low)
- ❌ Slower responses (~200-300ms more)
- ❌ Occasional over-explanations

---

## Token Cost Comparison

**Current Implementation (Conservative Helpful):**
- 3 chunks × 1200 chars = 3,600 chars (~900 tokens for knowledge)
- 10 turns × ~50 tokens = ~500 tokens for history
- System prompt: ~400 tokens
- **Total input: ~1,800 tokens per request**
- **Output: ~200 tokens** (at 800 max_tokens avg ~200 used)
- **Cost: ~$0.002 per request** (at $0.001/1K input, $0.002/1K output)

**Aggressive Config:**
- 4 chunks × 1400 chars = 5,600 chars (~1,400 tokens for knowledge)
- 12 turns × ~50 tokens = ~600 tokens for history
- System prompt: ~400 tokens
- **Total input: ~2,400 tokens per request**
- **Output: ~250 tokens**
- **Cost: ~$0.0029 per request**

**Difference: +45% cost, +40% token usage**

---

## Recommendation

**Start with the CURRENT implementation** (conservative helpful).

**Monitor for 1-2 weeks:**
- Check escalation rate
- Review "I don't have information" frequency
- Measure user satisfaction
- Analyze token costs

**Then decide** if you need more aggressive settings.

Most support systems work well with conservative helpful mode.
Only go aggressive if you see:
- High escalation rate (>30%)
- Many "incomplete answer" complaints
- Complex product requiring detailed explanations
