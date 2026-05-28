# Why we shipped Phase A as an engagement layer (not "businesses near you")

**Date:** 2026-05-08
**Audience:** Internal — to share the strategic call behind `/app/explore` Phase A
**One-line takeaway:** We invested in signals we already have (follows, recent chats, industry category) instead of signals we'd have had to build (geography). It's a smaller, faster, more measurable bet that compounds with our positioning.

---

## The proposal we considered

Add a "Businesses near you" surface to `/app/explore` — a 10 km geographic feed that helps customers discover local businesses on the platform.

It's a defensible idea. Geo discovery is intuitive, customers understand it instantly, and it's the obvious answer to "what should we put on the explore page?"

## Why we said no — for now

Three things stood out when we put it under load:

1. **Luran's positioning is engagement + automation, not directories.** Our marketing surface (`ProductOverview`, `AudienceSwitcher`, the new `/register` flow) sells AI customer support and customer engagement. A geo-first explore page would point in the opposite direction — toward Yelp/Google Places semantics — and would compete with our own message.

2. **The data wasn't there.** The `Tenant` model has zero geographic fields today. To ship "near you" honestly we'd need address capture in onboarding, geocoding, lat/lon storage, distance queries on every request, and a privacy model for customer location. That's weeks of infrastructure work before the first usable feature.

3. **The data we *do* have was unused.** Every relationship signal we'd want for ranking — who follows whom, when each customer last chatted with each business — was already in the database, untouched by `/app/explore`. The page rendered an alphabetical list with a featured boost. No relevance, no awareness of who was browsing.

We were about to spend our roadmap budget chasing a missing capability instead of activating one we'd already paid for.

## What we shipped instead

Phase A delivers four things, all built from existing data:

| What | Why it matters |
| --- | --- |
| **Personal block** — up to 8 businesses you already follow or have recently chatted with, ranked by last activity | Turns the explore page from "find a stranger" into "pick up where you left off." No clutter when empty. |
| **12-category taxonomy + filter chips** | One controlled vocabulary across the system. Lets customers narrow the browse grid; lets us measure which verticals actually drive engagement. |
| **Conversation source attribution** (`source` + `source_meta` on `conversation_sessions`) | Every chat created from explore now records *where* the user came from — personal block vs browse vs search vs direct widget — and the active filter state at click time. We can answer "did the personal block lift engagement?" with a SQL query, not a guess. |
| **Dashboard category nudge** — dismissible banner on `/dashboard` for tenants without a category set | A 5-second action with a clear payoff (better placement) instead of yet another modal. |

## What this buys us

- **Time to first measurable value.** The personal block was wired up against existing tables. Geo would have started with a schema migration and an address-capture UX. We landed Phase A in three plans of independent tasks across three repos with subagent-driven execution; geo Phase A would have been twice the surface area before any user saw a benefit.

- **A measurement spine.** Source attribution is the part that compounds: every future explore experiment (different ranking signals, different layout, different copy) becomes A/B-evaluable because the attribution columns are already present on every conversation. Without this, every "did X work?" turns back into vibes.

- **A clear next-decision gate.** We didn't kill geography — we deferred it behind evidence. If categories, the personal block, and source attribution surface a real pattern of unmet local demand, we'll know, and the decision to add geo will be data-led instead of intuition-led. If they don't, we saved several weeks.

- **Product coherence.** Phase A's surface area maps cleanly onto how we already talk about Luran: a network of businesses with persistent customer relationships. "Pick up where you left off" reinforces that. "Businesses 8 km away" doesn't.

- **Reversibility.** Nothing in Phase A blocks a future geo layer. The category column is independent of address. The personal block ranking is a single sort key we can extend. The attribution columns are forward-compat (they store unknown enum values via a coerce-on-unknown pattern, so a future `EXPLORE_NEARBY` source slots in without a migration).

## What we explicitly did not do

- No geographic data of any kind (lat/lon, addresses, "near me", region tags).
- No multi-category support — one primary category per tenant.
- No follow-velocity / chat-velocity ranking — straight `last_activity_at` desc.
- No third-party analytics SDK — we own the source columns end-to-end.
- No category-management CMS — taxonomy is hard-coded in 13 values (12 + "other"). Easy to evolve when we have evidence of need.

This is deliberate: each "no" is a knob we can turn later if the data tells us to. We're not building for hypothetical future requirements.

## Bottom line

Phase A isn't a pivot away from local discovery. It's a smaller, faster, more measurable bet placed on signals already in the database, designed so the next decision — including a geo layer if the evidence calls for it — is data-led. The associate's instinct that explore needed *something* better than alphabetical was correct. The choice we made was about *which* something paid back fastest.
