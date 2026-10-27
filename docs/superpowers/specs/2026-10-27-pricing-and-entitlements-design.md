# Pricing and entitlements: prod evidence and proposal (2026-10-27)

Status: PROPOSAL for Adebayo's decision. Nothing here is built. Evidence is read-only aggregates pulled from prod on 2026-10-27 (no names, emails or message content). Earlier code review: plans differ on volume plus 5 flags; sales agent, CRM, leads, recordings, ratings, image upload are not gated (see the 2026-10-27 review).

## 1. What prod actually shows
| Fact | Value |
|---|---|
| Tenants in total | 13 (1 growth, 10 trial_expired, 2 trialing). **No Lite or Professional tenant exists** |
| Payments ever | **0** (`payment_transactions` is empty). The single Growth tenant is a manual admin override |
| Signups | Feb 4, Mar 1, Apr 3, May 2, Jun 1, Oct 2: about 1.6 a month |
| Winback (Jun-Jul 2026) | 7 Professional grants, all expired, **0 converted** |
| Auto-signup grant | disabled |
| Trial tenants that uploaded a KB source | **0 of 12** |
| Sales agent configured | **0** (`tenant_sales_agent_config` has no rows) |
| Voice, CRM/Slack, recordings, AI actions in use | 0 (webhooks/lookup chains enabled on the one Growth override) |
| Biggest tenant (the Growth override, 252 days old) | 54 conversations ever, **17 in its peak month**, 1 user, 11 KB sources, 5 flows, 9 tickets |
| Everyone else | 1 to 3 conversations ever, 6 of the 10 expired trials never had one |
| Team | 13 admins, 1 support agent in total |

## 2. What it means
1. **Nobody is near any limit.** Peak is 17 conversations in a month against Lite's 2,000. Plan limits do not shape behaviour at today's scale.
2. **The funnel breaks before value.** No trial tenant reached a knowledge base. Known contributors (all documented): the install snippet never started the widget until 2026-10-16; text KB upload stores no vectors; short documents fail silently; tenants created before a KB default to "human first" so the AI is silent. Causality is plausible, not proven.
3. **There is no price evidence at all.** Zero payments means ₦9k, ₦40k and ₦80k are untested hypotheses. D-Zero's published ₦40k Starter (WhatsApp only) shows the market ceiling is higher than Lite.
4. **Now is the cheapest moment to add gates.** No paying customer can be harmed; the one Growth override keeps everything.

## 3. Proposed tier matrix (hypothesis, validate before launch)
Principle: gate what costs Luran money per use or what larger or more sophisticated businesses need; never gate what makes the core promise work; the trial shows everything.

| Capability | Trial (30 d) | Lite | Professional | Growth |
|---|---|---|---|---|
| Price per month | free | **₦15,000** (was 9,000) | ₦40,000 | ₦80,000 |
| Conversations / month | 200 | 2,000 | 10,000 | 25,000 |
| Team seats | 1 | 3 | 10 | 20 |
| Web widget + hosted page | yes | yes | yes | yes |
| WhatsApp (linked device beta) | 1 number | 1 number | 2 | 5 |
| Human handoff, tickets, agent app | yes | yes | yes | yes |
| Image upload | yes | yes | yes | yes |
| Rating collection | yes | yes | yes | yes |
| Ratings breakdown and CSV export | yes | no | yes | yes |
| Lead capture, scoring, email notice | yes | yes | yes | yes |
| Meeting booking, quotes | yes | no | yes | yes |
| Slack and CRM webhook | yes | no | yes | yes |
| Screen recording | yes | no | yes | yes |
| AI actions in chat | 2 | 2 | 15 | 40 |
| Lookup chains, webhook flow nodes | no | no | yes | yes |
| Analytics | no | yes | yes | yes |
| Voice | parked | parked | parked | add when launched |
Why Lite ₦15k: it now includes WhatsApp, which is the headline value; ₦15k to ₦40k (2.7x) is explained by real capability gates (close the sale, integrate, record, more seats). Raise Lite only for NEW signups. Validate with 5 customer calls (see section 6).

## 4. Mechanism and grandfathering
- Today limits live in two places (`subscription_plans` and `tenant_capabilities`) and drifted once (plan strings, 2026-10-16). Prefer ONE entitlements source: plan row -> resolved entitlements (plan defaults + `override_flags` + promo grants), cached per tenant. New flags: `can_use_sales_close` (booking + quotes), `can_use_crm_notifications`, `can_export_ratings`, plus plan-driven values for the existing `can_use_screen_recording`, `can_use_ai_chat_automation`, `max_whatsapp_numbers`.
- Enforcement points to add: meeting booking and quote paths (`chat.py` sales branches, `meetings.py`), Slack/CRM in the sales notification worker, ratings export endpoint, screen-recording gate (`services/screen_recording/gating.py`), AI-chat automation in the orchestrator, WhatsApp connection create.
- Dashboard: lock badges and one upgrade prompt per locked feature (today nothing is locked in the sidebar and Trial users hit a 403 on Analytics).
- **Grandfathering is trivial today:** the only tenant on a paid tier is the Growth override, which keeps all features via `override_flags`. Trial-expired tenants hold no entitlements. Winback grants are expired. Do it now, before the first paying customer.
- Metering for WhatsApp: the backend reuses one session per chat until resolved, so one customer chat can count as a single "conversation" for months. Define a WhatsApp conversation as a 24 h window (rotate sessions on idle) before charging on it.

## 5. Order of work (estimate, agent-days)
1. Entitlements source + plan seed + sync + migration: 1.0
2. Enforcement at the six points + tests: 1.5
3. Dashboard lock badges and upgrade prompts: 1.0
4. Pricing page, docs, plan strings, SOP/FE guide: 0.5
Total about 4 agent-days. **Do activation first** (section 6): gates change nothing while nobody reaches value.

## 6. Do first: activation (cheaper than gating, evidence says it is the bottleneck)
1. Onboarding checklist in the dashboard: install the widget (verify it actually loads), upload a first KB, send a test message; show "AI is silent because Who answers first = human".
2. Fix the KB traps: index text uploads, reject short documents with a clear message, chunk guidance.
3. Call the 4 expired-trial tenants that had conversations (contact details are NOT in this doc; Adebayo pulls them): why did they stop, what would they pay for.
4. Decide who the Growth override tenant is (internal or design partner) so it can be labelled.
5. Reconsider the 30-day clock: 10 trials expired, 6 never started; consider starting the clock at first KB upload.

## 7. Decisions needed from Adebayo
(a) Lite price change to ₦15,000 for new signups. (b) Approve the matrix, especially putting booking, quotes, CRM and recording at Professional. (c) Activation first, gating second? (d) Who is the Growth override tenant. (e) WhatsApp conversation definition (24 h window).
