# Luran FAQ (Implementation-Aligned)

Last updated: February 10, 2026

## 1) What is Luran?
Luran is a multi-tenant customer support platform that combines AI/RAG chat, flow automation, and human escalation.

## 2) Which AI provider/models are used now?
Current backend defaults are OpenAI:
- `gpt-4o-mini` for generation
- `text-embedding-3-small` for embeddings

## 3) Does Luran support guided button-style support flows?
Yes. Flows can present options and branch by selected response (for example MTN-style guided journeys).

## 4) If no flow matches, what happens?
The system falls back to AI/RAG chat, using active knowledge sources.

## 5) Can users still reach a human agent?
Yes. Escalation can happen from flow nodes or AI support logic, and can create tickets for support follow-up.

## 6) What file types can be uploaded to knowledge base?
Supported: `.txt`, `.md`, `.pdf`, `.docx`.

## 7) What is the file size limit?
10 MB per file.

## 8) Are limits enforced by subscription plan?
Yes. Runtime checks enforce tenant capabilities such as conversations/month, knowledge storage, source count, flows, and webhook access.

## 9) How do I embed the widget?
Use the widget snippet with `window.luranSettings`:

```html
<script>
  window.luranSettings = {
    tenantId: 'YOUR_TENANT_UUID',
    apiKey: 'YOUR_PUBLIC_API_KEY'
  };
</script>
<script src="https://cdn.luranai.com/widget/v1/widget.js" async defer></script>
```

## 10) Which API key should be used in frontend widget code?
Only PUBLIC key (`pk_live_...`). Never expose secret keys in browser code.

## 11) Does conversation continue after browser refresh?
Current chat client stores session state in browser storage and restores by tenant/session context. If storage is cleared or expired, a new session may start.

## 12) What are current default subscription tiers?
`trial`, `lite`, `professional`, `growth`, `enterprise`.

## 13) What is trial duration?
Self-service registration currently sets trial end to +30 days.

## 14) Why can a request fail with 402?
Typical reasons:
- trial expired
- subscription inactive/canceled/past_due
- conversation quota reached

## 15) Why can knowledge upload fail with 403?
Typical reasons:
- storage limit reached
- knowledge source count limit reached

## 16) Why can widget config calls fail with 401/403?
- 401: API key invalid
- 403: API key does not belong to tenant being requested

## 17) Does Luran include CORS controls?
Yes. CORS is configured by `BACKEND_CORS_ORIGINS` and enforced in backend middleware.

## 18) Does dashboard need to mirror backend flow schema exactly?
Yes. Node config keys and edge shape should stay in parity with backend execution schema to avoid runtime mismatches.

## 19) Can one tenant have multiple active flows?
Yes. Multiple active flows can coexist. Router behavior decides which one to start when a welcome/global trigger is used.

## 20) Can a tenant hide some flows from auto-router menu?
Yes. Flows can set router-related config (for example `exclude_from_router`) so only intended customer-facing flows appear.

