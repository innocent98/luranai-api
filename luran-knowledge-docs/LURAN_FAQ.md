# Luran FAQ (Tenant Version)

## 1) What is Luran?
Luran is a customer support platform that combines:
- AI answers from your knowledge base
- Guided flows for repeatable support paths
- Human handoff when needed

## 2) How fast can I get started?
Most teams can go live in minutes:
1. Create account and verify email
2. Upload knowledge files
3. Configure widget branding
4. Add widget script to website

## 3) What files can I upload to Knowledge Base?
Supported file types:
- `.txt`
- `.md`
- `.pdf`
- `.docx`

Max file size is `10 MB` per file.

## 4) What happens after I upload a file?
Uploads are processed in the background:
- text is extracted
- content is chunked
- source moves to `ACTIVE` when ready

If processing fails, source status becomes `FAILED`.

## 5) How does the AI answer questions?
The AI uses your uploaded knowledge to answer customer questions.  
If the question is outside your available knowledge, it may ask for clarification or guide the user toward escalation.

## 6) Can I still use flows?
Yes. Flows are great for structured support journeys (menu-style support, troubleshooting steps, guided options).  
If no flow handles a question, chat can continue with AI.

## 7) Can customers talk to a human agent?
Yes. Handoff can happen from flow steps or AI escalation logic.  
When escalated, a support ticket can be created and agents can continue in dashboard.

## 8) How do I embed the widget?
Use your install snippet and place it before `</body>`:

```html
<script>
  window.luranSettings = {
    tenantId: 'YOUR_TENANT_UUID',
    apiKey: 'YOUR_PUBLIC_API_KEY'
  };
</script>
<script src="https://cdn.luranai.com/widget/v1/widget.js" async defer></script>
```

Use public key only (`pk_live_...`).

## 9) Why is widget setup failing?
Most common causes:
- wrong `tenantId`
- wrong public API key
- source still processing
- plan limits reached
- subscription inactive

## 10) What are the current plan tiers?
Current tiers:
- `trial`
- `lite`
- `professional`
- `growth`
- `enterprise`

## 11) How long is trial?
Trial is currently `30 days` from signup.

## 12) What happens when trial or subscription expires?
Chat/support capabilities are restricted until the subscription is reactivated.

## 13) Why can uploads fail with limit errors?
Limits are enforced per plan, including:
- total storage (MB)
- number of knowledge sources

If you hit a limit, reduce usage or upgrade plan.

## 14) Can one tenant run multiple flows?
Yes. You can have multiple active flows.  
Use clear flow titles and menu labels so customers can choose the right path.

## 15) Can I hide some flows from customer menu?
Yes. Internal flows can be excluded from the customer router menu.

## 16) Is conversation preserved on refresh?
The chat widget restores recent session context from browser storage (within session/TTL rules).  
If storage is cleared or expired, a new session starts.

## 17) What are current starter pricing references?
Current public pricing references:
- Lite: ₦9,000/month
- Professional: ₦40,000/month
- Growth: ₦80,000/month
- Enterprise: custom pricing

For latest plan details, always confirm from the Pricing page.

