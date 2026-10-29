# Messenger and Instagram channels: requirements memo (2026-10-29)

Status: **research memo for Adebayo's decision. Nothing is built.** Facts come from Meta's developer documentation and third-party guides found on 2026-10-29. Items marked **verify** were reported by third parties or could not be confirmed in Meta's own pages; confirm them in the Meta App Dashboard before committing to a plan. Meta changes these rules often.

## 1. What this is

Extend Luran's AI support agent from the web widget and WhatsApp to Facebook Messenger and Instagram direct messages. Unlike our WhatsApp quick-connect (a linked-device route), these two have an **official** API only, so there is no ban risk, but access is gated by Meta's reviews.

## 2. What Luran needs to have (once, as the platform)

| Requirement | Detail | Owner |
|---|---|---|
| Meta developer app | Business-type app with the Messenger Platform and Instagram messaging use cases added | Adebayo / engineering |
| Business Verification | Required for Advanced Access (Meta: Advanced Access "requires App Review and Business Verification") | Adebayo (company documents) |
| App Review | Per permission, with a screencast and test instructions for the reviewers; Meta warns that requesting unneeded permissions is a common rejection reason | Engineering + Adebayo |
| Tech Provider status and Access Verification | Needed to serve businesses we do not own (every Luran tenant): verify the business, verify it may access other businesses' data, complete App Review. Reported timings: about 5 business days for Access Verification (verify) | Adebayo |
| Privacy policy URL, terms URL, data-deletion callback or instructions | Required on the app for review and live mode | Engineering (we already have the pages; the deletion callback is new) |
| HTTPS webhook with verify token and `X-Hub-Signature-256` check | Same shape as our existing signed webhooks | Engineering |

**Access levels.** Standard Access is enough only for accounts we or our app testers own. Serving tenants needs **Advanced Access**, which is what App Review and Business Verification unlock. Until approved we can only build and test against our own Page and Instagram account.

**Timing.** Business Verification and the reviews are the long pole: plan for weeks, not days (one guide quotes roughly 2 to 4 weeks for permission review; **verify**). Rejections and resubmissions are common.

## 3. Permissions we would request (names to confirm in the dashboard)

| Channel | Likely permissions | Why |
|---|---|---|
| Messenger | `pages_messaging`, `pages_show_list`, `pages_manage_metadata` | receive and send messages on a tenant's Page, list the Pages a tenant can connect, subscribe the webhook |
| Instagram | `instagram_basic`, `instagram_manage_messages`, plus the Page permissions above | read the connected professional account, receive and reply to DMs |
| Optional | `business_management` | only if we manage assets through the tenant's Business portfolio |
| Human handover | the Human Agent feature | lets a person reply after the standard window (see section 5); needs its own App Review |

Forum reports describe an "Application does not have capability" error for Instagram even after the permission shows approved, apparently tied to the app's use-case configuration and verification state (**verify**, unconfirmed user reports).

## 4. What each tenant needs

- **Messenger:** a Facebook Page they administer.
- **Instagram:** an Instagram **Professional** account (Business or Creator) linked to a Facebook Page, with "Allow access to messages" enabled in the Instagram settings.
- A tenant admin connects through a **Facebook Login** flow in our dashboard, grants the permissions and picks the Page; we store a Page access token per tenant, encrypted, and subscribe the Page to our webhook.
- For us to serve a tenant's account without that tenant being an app tester, the app must be on Advanced Access and live.

## 5. Messaging rules that shape the product

- **24-hour standard window:** a business may reply within 24 hours of the customer's last message; each new customer message resets it.
- **Human Agent tag:** extends the window to **7 days**, but only for replies sent by a **person**. Automated or AI replies do not qualify. It needs App Review and is not available in development mode.
- **Consequence for the AI:** after 24 hours of silence from the customer, the AI cannot message first. Our existing "reply-only" behaviour for WhatsApp (24-hour window, no outbound templates) already matches this.
- **Instagram:** the customer must message first.
- A third-party article claims a 2026-09-28 extension to 7 days for chats started from Click-to-Messenger ads (**verify** in Meta's changelog; it is separate from the Human Agent tag).
- Platform rate limits and content rules apply; we would need to read the current limits before sizing.

## 6. How it fits our code

- `app/channels/` already has a channel registry and a `cloud_api` stub slot. Messenger and Instagram would be two new providers: an inbound webhook that maps a sender (page-scoped ID) to a conversation session with `source='messenger'` or `'instagram'`, and an outbound sender that checks the 24-hour window.
- Reuse: the conversation pipeline, AI reply, takeover, ratings, image handling (moderation and Cloudinary), plan limits and entitlements. Page-scoped IDs are not phone numbers; identity and the "exclude contact" idea need rethinking.
- New: Facebook Login connect flow and token storage/refresh in the dashboard and backend, webhook verification, per-tenant Page subscription management, data-deletion callback, dashboard Channels pages, entitlement gates per plan.
- Size (rough, not estimated against the code): one backend channel plus a dashboard connect flow per platform, sharing about half of the work between Messenger and Instagram. Review and verification time dominates the calendar, not engineering.

## 7. Shared work with WhatsApp Option B

The official WhatsApp Cloud API route (our Option B memo, `docs/whatsapp-option-b-decision-memo.md`) needs the same Meta pieces: a Business-type app, Business Verification, Tech Provider status, Embedded Signup via Facebook Login for Business, App Review. **One verification effort unlocks all three channels.** If we pursue Messenger and Instagram, start the Meta business setup once and decide per channel which permissions to request.

## 8. Recommended sequence

1. **Owner actions now (cheap, long lead time):** Meta Business portfolio, Business Verification, create the Meta developer app (Business type). These are the same as Option B's owner track.
2. Build Messenger first (simpler: Page-based) against our own test Page on Standard Access, then Instagram.
3. Prepare App Review material: screencasts, test instructions, privacy and deletion URLs; request only the permissions we use.
4. Apply for Tech Provider / Access Verification and Advanced Access; ship to tenants behind a plan gate once approved.

## 9. Open questions for Adebayo

1. Which channel first, Messenger or Instagram? (Instagram DMs are usually what small businesses in our market care about; Messenger is the simpler build.)
2. Are you willing to start Business Verification now, since it is the same effort Option B needs?
3. Should the AI be allowed to answer on these channels with no human monitoring, given the 24-hour window and the Human Agent rule?

## Sources

- Meta, [App Review for Instagram API](https://developers.facebook.com/docs/instagram-platform/app-review)
- Meta, [Instagram Platform overview](https://developers.facebook.com/documentation/instagram-platform/overview)
- Meta, [Messenger Platform overview](https://developers.facebook.com/documentation/business-messaging/messenger-platform/overview)
- Meta, [Send a Message (Messenger Platform)](https://developers.facebook.com/documentation/business-messaging/messenger-platform/send-messages)
- Meta, [Become a Tech Provider](https://developers.facebook.com/documentation/business-messaging/whatsapp/solution-providers/get-started-for-tech-providers)
- Third-party (verify before relying): [Twilio Tech Provider FAQ](https://twilio.com/docs/whatsapp/tech-provider-program/faq), [360dialog guide](https://docs.360dialog.com/partner/partner-program/meta-tech-provider-program/becoming-a-meta-tech-provider-a-step-by-step-guide), [Meta messaging window explainer](https://singhamandeep.com/?p=1404), [Spurnow on the ad-started 7-day window](https://www.spurnow.com/en/blogs/instagram-messenger-ads-7-day-messaging-window)
