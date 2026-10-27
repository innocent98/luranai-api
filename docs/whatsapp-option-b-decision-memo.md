# WhatsApp option B (official Meta Cloud API): decision memo — 2026-10-27

Status labels: **VERIFIED** = read on the primary page or file today. **UNVERIFIED** = secondary source only. **ESTIMATE** = our judgement. Raw research with URLs: `side-session-notes/.../optionB-factsheet.md` (outside the repo); the key URLs are repeated inline below.

## 1. Recommendation
1. **Apply to become a Meta Tech Provider directly, starting now.** It has no fee that we could find, no per-message markup, and keeps the connect UX inside the Luran dashboard (Embedded Signup). Calendar time is the cost: Meta publishes no timeline, so plan **2-6 weeks (ESTIMATE)**.
2. **Keep a BSP as the fallback, not the plan.** If approval drags past ~4 weeks, **360dialog** is the best documented stopgap (fixed fees, "zero message markup" claim, hosted Embedded Signup for partners). Twilio adds $0.005 per message on top of Meta's fees, which is a poor fit for a high-volume AI bot.
3. **Target Embedded Signup v4 or v5, never v2/v3.** Meta retired v2/v3 on **2026-10-15** (they are now served as v4); v4 is current and **v5 was released 2026-10-20** (WhatsApp Business app onboarding on by default) (VERIFIED, <https://developers.facebook.com/docs/whatsapp/embedded-signup/versions>). The earlier "V2 retires Oct 2026" report was right; V4 is the safe minimum.
4. **Do not promise Coexistence for Nigeria.** Unconfirmed, leans unsupported (section 5).
5. **Surface the cost and billing reality to tenants before selling option B**: service replies are no longer free past 1,000/number/month, and Meta bills in USD-class currencies only (no NGN). See sections 7-8.

## 2. Tech Provider vs BSP
| | Tech Provider (direct) | BSP / Solution Partner route |
|---|---|---|
| Who we are to Meta | Tech Provider; **no credit line**; each tenant adds its own payment method and Meta bills the tenant directly (VERIFIED, Meta "Partner Terminology" page) | Vendor is the Solution Partner (has a credit line, can invoice); we integrate on its API |
| Per-message markup | None from Meta to us. **Terms 4.1(c): the Platform may not be resold or rented to third parties**, so do not assume we can add a markup to Meta's fees (VERIFIED paraphrase; get counsel's read) | 360dialog: fixed subscription, "zero message markup" (its own page). Twilio: +$0.005/message. Gupshup: extra 6% on some marketing (UNVERIFIED) |
| Fixed cost | None found on Meta's page (absence not proof) | 360dialog API Regular EUR49/number/mo; Partner Platform Starter EUR250/mo + EUR49/channel, Growth EUR500 + EUR25, Premium EUR1,000 + EUR15 (VERIFIED, <https://360dialog.com/pricing>). Termii: $50/WhatsApp account/month, USD (VERIFIED, <https://termii.com/pricing>) |
| Tenant UX | Embedded Signup popup inside the dashboard (tenant logs in with Facebook, picks or creates the business portfolio and WhatsApp account, verifies the number); localised in 30 languages | Same popup if the BSP exposes Embedded Signup to partners (360dialog does); otherwise a hosted page |
| Control and lock-in | Full: tokens, templates, webhooks are ours | Dependent on vendor limits and pricing; WABA migration between partners exists but is a project |
| Onboarding cap | 10 new customers per rolling 7 days until Business Verification + App Review + Access Verification, then 200/7 days (VERIFIED, Embedded Signup overview) | 360dialog: 200 clients/7 days per Solution ID; up to 3 numbers before Tech Provider registration |
| Calendar | Business verification (days, uncertain for Nigeria) + App Review (~5 business days per community reports, UNVERIFIED) + Access Verification; **ESTIMATE 2-6 weeks** | Days, if the BSP will onboard us as an ISV; BSP sales cycle applies |
| Nigeria / naira | Meta bills in 16 currencies; **NGN is not one of them** (VERIFIED, pricing page) | No vendor read showed NGN invoicing (Termii shows a naira toggle but no naira amounts). Ask in writing |
**Best tenant UX:** Tech Provider with our own Embedded Signup (one flow, Luran-branded, no third-party login). A BSP with hosted Embedded Signup is nearly as good. Unverified vendors (Gupshup, Infobip, Sendchamp, Bird, Kaleyra, Yellow.ai) were not assessed and should not be chosen on this memo.

## 3. What a Tech Provider needs (VERIFIED, <https://developers.facebook.com/documentation/business-messaging/whatsapp/solution-providers/get-started-for-tech-providers>)
1. A Meta app (Business type) with the WhatsApp use case, attached to a **Meta Business portfolio**.
2. **Business Verification** of that portfolio (name, address, phone, email, website; documents if Meta cannot find the business). Meta lists no country requirement; the Nigerian document list was **not found** (a vendor list of 15 countries excludes Nigeria; UNVERIFIED).
3. **App Review**: app icon, privacy policy URL, category, **two screen-recorded videos** (a message sent from our app received in WhatsApp; our app creating a message template); advanced access to `whatsapp_business_messaging` and `whatsapp_business_management`.
4. **Access Verification**: Meta's 2025-10-03 changelog says it is no longer required to *become* a Tech Provider, but without it the 10-customers-per-week cap stays. Its procedure page could not be read (NOT VERIFIED).
5. Embedded Signup: HTTPS domain, Facebook Login for Business configuration, `FB.login` with `response_type: code`; the code is valid 30 s; exchange for the tenant's business token; `POST /<WABA_ID>/subscribed_apps`; `POST /<PHONE_NUMBER_ID>/register` with a 6-digit PIN; the tenant must add a payment method in WhatsApp Manager before it can send.
6. Webhooks: subscribe to `account_update`, messages and statuses.
Not found: a Meta fee for the programme; ToS and data-deletion URL requirements for this flow (standard Meta app settings probably apply; UNVERIFIED).

## 4. Messaging rules that shape the product
- **24-hour customer service window** (VERIFIED): opens when the customer messages, restarts on each customer message; inside it any message type; outside it only approved templates, recipients must have opted in. An AI support reply inside the window is a **service message**.
- **Templates**: Authentication / Marketing / Utility; automatic review, "can take up to 24 hours"; category changes to marketing are now automatic.
- **Messaging limits (current model, VERIFIED, changelog Oct 8 2025)**: per **business portfolio**, not per number. A new portfolio starts at **250 unique users per 24 h** (outside service windows); it moves to **2,000** by verifying the business or by sending 2,000 template messages to unique users in 30 days with good quality; then automatic scaling to 10K, 100K, unlimited. The old "unverified 1K tier" is superseded. Replies inside the 24 h window to people who messaged first do not need these tiers; they matter for templates/outreach.
- **Display name**: required at registration; review is automatic and starts when a higher limit is reached; no time stated; up to 10 changes per 30 days.
- **Official Business Account (blue check)**: needs a verified portfolio, 30 days on platform, two-step verification, approved display name; not available to numbers that also use the WhatsApp Business app (so not with Coexistence).

## 5. Coexistence (WhatsApp Business app + Cloud API on one number)
VERIFIED from Meta (<https://developers.facebook.com/documentation/business-messaging/whatsapp/embedded-signup/onboarding-business-app-users>): offered by Solution Partners and Tech Providers via Embedded Signup; Business app **version 2.24.17 or higher**; contacts/history sync must happen within 24 h of onboarding; history covers up to 180 days; throughput 20 mps; app-sent messages are free, Cloud API sends follow Cloud API pricing; the 24 h window applies only to Cloud API messages. Restrictions: no groups or calls on the API side; disappearing messages, view-once, live location and broadcast lists are disabled; Windows/WearOS companions unsupported; a dual-use number cannot be deregistered; primary phone inactivity (~14 days) disconnects it.
**Nigeria (+234): UNCONFIRMED, leans not supported.** Meta's pages list no countries (and its changelog entries name Australia, Japan, Philippines, Russia, South Korea, Turkey, EEA/EU, UK, India; not Nigeria). Wati (Aug 2026) and HighLevel (Sep 2026) say Nigeria and South Africa are unsupported (UNVERIFIED but recent); one article contradicts itself. **Test empirically** with a real +234 number on a Tech Provider app before any promise to tenants.

## 6. Meta's policy on AI bots
Source: Meta Terms for WhatsApp Business Platform, section 4.7 "AI Providers", last modified 2026-09-23 (VERIFIED, <https://www.facebook.com/legal/Meta-Terms-for-WhatsApp-Business-Platform>); Meta's AI-provider pricing page says the terms were updated **2026-01-15** (VERIFIED, <https://developers.facebook.com/documentation/business-messaging/whatsapp/pricing/ai-providers>).
- **Banned**: AI/ML providers (LLMs, generative-AI platforms, general-purpose assistants) making their technology available on the Platform where that is the **"primary (rather than incidental or ancillary) functionality"** (exact fragment, VERIFIED). Meta decides what is primary. Carve-outs exist where the law forces Meta to allow it (Italy, EU/EEA, Brazil, Türkiye); in Brazil Meta charges AI providers per service message.
- **Allowed**: a business using an AI provider as its Solution Provider, provided Platform data (even derived) is **not used to train or improve AI models**; the only exception is fine-tuning a model for that business's own exclusive use. Business Messaging Policy (VERIFIED, last updated 2026-09-23): automation is allowed inside the window with **clear escalation to a human**, opt-in required, no impersonation.
- **Reading for Luran (ESTIMATE, not legal advice)**: a tenant's customer-service agent, where the AI is incidental to that tenant's business, appears to be in scope; Luran as a multi-tenant AI vendor is exposed to the "primary functionality" test. No Meta text explicitly blesses support bots. Design rules: no general-purpose open assistant mode; no use of WhatsApp content to train shared models (check the LLM sub-processors' data-use terms); always offer human escalation; get counsel and/or Meta's written view before launch.
- This does not affect option A legally in the same way (it is outside the Platform entirely), but option A has its own ToS problem.

## 7. Official NGN rate table (Meta rate card, effective 2026-10-01)
Source: Meta rate-card workbook "USD list rates" linked from <https://developers.facebook.com/docs/whatsapp/pricing#rate-cards-and-volume-tiers> (downloaded and parsed 2026-10-27; the file is served as `.csv` but is an Excel workbook; sha256 `3e996b56a86d9e7f70a9cfded896c8f39a19cb0e88683ed06b28f191856c9260`, title row "Cost per message in USD on the WhatsApp Business Platform, effective October 1, 2026"). **Nigeria is a standalone market (calling code 234)**, not "Rest of Africa". Meta publishes Nigeria in **USD only**; the Naira column below is our conversion.

| Category (per delivered message, Nigeria +234) | Meta rate (USD) | Naira at N1,332.49/USD |
|---|---|---|
| Marketing | $0.0516 | **N68.76** |
| Utility (template) | $0.0067 | **N8.93** |
| Authentication | $0.0067 | **N8.93** |
| Authentication-international | $0.0750 | N99.94 |
| **Service (non-template reply in an open 24 h window)** | **$0.0067** | **N8.93** |
| Meta Business Agent | $2.00 per 1M tokens | n/a |

Exchange rate: official NFEM closing rate **N1,332.49/USD on 2026-10-09**, as reported from CBN data by Techeconomy (secondary, UNVERIFIED against CBN directly; <https://techeconomy.ng/dollar-to-naira-exchange-rate-today-october-12-2026>). Parallel market was ~N1,380 selling the next day; costs are ~3.6% higher at that rate. Tenants pay Meta by card in a supported currency, so the real naira cost depends on their bank's rate.
**The press figures (about N14 per service/utility and N84 per marketing at N1,340) do not match Meta's card** (N8.93 and N68.76) and should be discarded.

Other pricing rules (VERIFIED, <https://developers.facebook.com/docs/whatsapp/pricing>):
- Per-message pricing since 2025-07-01 (not per conversation); one category per message; only delivered business-to-user messages are charged; **user-to-business messages are free**.
- **1,000 free service messages per business phone number per month**, no rollover, resetting at 00:00 in the Messaging account's timezone.
- **Free entry point (click-to-WhatsApp ad)**: when a user messages from a CTWA ad, a window of **up to 7 days** opens in which marketing, utility, authentication and service messages are free.
- **Oct 1 2026 changes**: Meta resumed charging for service messages beyond the 1,000-message tier and for utility templates sent inside an open service window (both were previously free). Businesses and solution providers without a payment method on file by 2026-09-30 may have service messages stop delivering. Gov/non-profit exemption lasts to 2027-12-31. Webhook `pricing.type` for charged service messages changes from `free_customer_service` to `regular`. 40 rates updated, 9 markets moved out of "Rest of" regions. Authentication/utility have volume tiers that start above 100,000 messages/month, which is irrelevant at our scale.
- Billing currency: **NGN is not a Meta billing currency** (16 supported: USD, AED, ARS, AUD, BRL, CLP, COP, EUR, GBP, IDR, INR, MXN, MYR, PEN, SAR, SGD).

## 8. Illustrative monthly tenant cost (one WhatsApp number)
Assumptions: every AI reply is one service message inside the 24 h window; no templates or marketing; one number; FX N1,332.49. Real bills rise if replies are multi-part. "Luran plan" uses Lite N9,000 and Professional N40,000 from `docs/SUBSCRIPTION_PLANS.md` (Feb 2026; may be stale).

| AI replies / month / number | Billable (after 1,000 free) | Meta fee, option B | Meta fee at N1,380 | Option A (linked device) Meta fee |
|---|---|---|---|---|
| 1,000 | 0 | **N0** | N0 | N0 |
| 3,000 | 2,000 ($13.40) | **N17,855** | N18,492 | N0 |
| 6,000 | 5,000 ($33.50) | **N44,638** | N46,230 | N0 |
| 12,000 | 11,000 ($73.70) | **N98,205** | N101,706 | N0 |

Total to the tenant (Luran plan + Meta fee):
| Replies | Option B on Lite (N9,000) | Option B on Professional (N40,000) | Option A on Lite | D-Zero Starter (N40,000, ~12,000 AI messages) |
|---|---|---|---|---|
| 1,000 | N9,000 | N40,000 | N9,000 | N40,000 |
| 3,000 | N26,855 | N57,855 | N9,000 | N40,000 |
| 6,000 | N53,638 | N84,638 | N9,000 | N40,000 |
| 12,000 | N107,205 | N138,205 | N9,000 | N40,000 |

Reading: D-Zero's published Starter works out to ~N3.33 per AI message all-in (N40,000 / 12,000; their Pro N75,000 credit count was not given, so it is not compared per message). Meta's service rate alone is **N8.93, about 2.7x that** once past 1,000 messages. Luran on the official API on Lite undercuts D-Zero Starter only below roughly **4,500 replies/month**; above it D-Zero is cheaper unless we pass through or absorb the difference. Option A has no per-message Meta fee, at the price of ban risk. D-Zero prices are as supplied in the brief and were **not verified** by this spike. Business implication: sell option B as "official, pay Meta directly", price Luran's own plan separately, and show tenants this table at connect time.

## 9. What Adebayo must do himself vs what engineering does
**Adebayo (owner) — needs a legal-entity admin:**
1. Make sure a Meta Business portfolio exists for Luran's company with Adebayo as admin (business.facebook.com); use the company's real registered name and address.
2. Start **Business Verification** in Business Settings > Security Center. Have ready (Nigeria list is not published, UNVERIFIED): CAC certificate of incorporation / RC number, proof of business address (utility bill or bank statement), a business email on the company domain, and a website that shows the same name. Verification contact steps (phone/email code) must be done by him.
3. Create the Meta developer account and a **Business-type app** with the WhatsApp use case (or approve engineering doing it under his admin), attach it to the verified portfolio, and add engineering as app admins.
4. Provide public **privacy policy and terms URLs** for the app (confirm the existing Luran pages; a data-deletion URL will point to the existing account-deletion flow).
5. Provide a **second, separate +234 test number** (not the spike number) and a **USD-capable card** for the test WhatsApp account, so Coexistence for +234 can be tested and a payment method exists before launch.
6. Get legal advice on Terms 4.1(c) (markup/resale) and 4.7 (AI providers); decide whether Luran will invoice message fees or tenants pay Meta directly.
7. Decide BSP fallback budget (360dialog Partner Platform from EUR250/month) if approval takes more than ~4 weeks; email 360dialog and Twilio to ask about NGN invoicing and Nigerian number onboarding.
**Engineering:** record the two App Review videos from staging; build Embedded Signup (v4/v5) config and callback, token exchange, `subscribed_apps`, number registration/PIN, webhook verification, template management, 24 h window enforcement, and the Cloud API adapter behind the `Channel` abstraction (task T14 in the plan); show Meta fees to tenants; run the Coexistence test on +234; run Access Verification once the app is approved.

## 10. Open questions (not resolved by this memo)
1. Real Tech Provider approval time for a Nigerian company; the Nigeria business-verification document list.
2. Coexistence on +234 (test it).
3. Access Verification procedure; Hosted Embedded Signup details; phone-number quality rating page (could not be read).
4. Naira invoicing at any BSP; how Nigerian SME cards behave on Meta billing.
5. Whether a Tech Provider can bill tenants for Meta fees (Terms 4.1(c)).
6. Meta changelog fetched ends 2026-05-12, so June-October API deprecations are unconfirmed.
7. D-Zero pricing and credit definitions (supplied, unverified).
