# Phase A — Better defaults for `/app/explore` (no geography)

**Status:** Approved spec, ready for implementation planning
**Date:** 2026-05-07
**Author:** Aubin Deffo + Claude (brainstorm)
**Repos affected:** `luran` (backend), `luran-dashboard`, `luran-chat`

---

## Why this spec exists

The customer-facing `/app/explore` surface in `luran-chat` is currently a paginated alphabetical list of publicly listed businesses with a `featured` boost — no relevance ranking, no categorization, no awareness of who is browsing. An associate proposed "businesses near you" (geo-based discovery within 10km).

After analysis (current product positioning, the `Tenant` model has zero geo fields, and the most predictive signals — follows and recent chats — are already in the database but unused), the strategic call is:

> **Luran is an engagement layer for digital + physical businesses; geography is secondary.**
> Phase A invests in the signals already at hand. Geography is explicitly deferred (and may never happen).

Phase A delivers four things:
1. A meaningful default ranking on `/app/explore` driven by follow + recent-chat history
2. Industry-vertical categorization with a filterable chip strip
3. Conversation source attribution so we can measure whether discovery surfaces actually lift engagement
4. A minimal dashboard prompt to encourage tenants to set their category

Phase A intentionally does *not* deliver: geographic features, multi-category, follow-velocity ranking, third-party analytics SDKs, or new infrastructure.

---

## Scope and non-goals

### In scope
- New default ranking surface for `/app/explore` (logged-in customer view)
- Industry-vertical taxonomy (13 values: 12 + Other), single primary category per tenant
- Two endpoints supporting the explore page (extension + new)
- Tenant settings — category dropdown
- Dashboard home — dismissible nudge banner for un-categorized listed businesses
- Conversation attribution via `source` + `source_meta`
- URL-state-driven filters on `/app/explore` (refactor of existing component)

### Explicitly out of scope
- Geographic data of any kind (lat/lon, addresses, "near me", region)
- Multi-category support (single primary only)
- Follow-velocity / chat-velocity ranking signals (Phase B trigger)
- Third-party analytics SDKs (PostHog etc.)
- Category-management CMS for platform admins (taxonomy is hard-coded)
- Real-time invalidation infrastructure for the personal block
- Sponsored placement / monetization hooks
- Personal-block "see more" CTA — hard cap at 8

---

## Locked design decisions

| # | Decision | Choice |
|---|---|---|
| 1 | `/app/explore` layout | Hybrid — personal block + browse grid (no horizontal shelves) |
| 2 | Taxonomy | Industry-vertical, closed list of 13 values, single primary, optional-but-prompted |
| 3 | Personal block composition | Follows ∪ recent chats (within 30d), sort by `last_activity_at`, cap 8, hide-when-empty |
| 4 | Browse grid | `featured DESC > created-in-last-30d DESC > display_name ASC`; category filters browse only; search filters both with hide-on-empty; page-based pagination |
| 5 | API shape | Two endpoints — extend `/api/v1/businesses`, add `/api/v1/customer/personal-businesses` |
| 6 | Dashboard category UI | Always-visible field on `/dashboard/settings/company` below `description`; dismissible nudge banner on `/dashboard` home |
| 7 | Metric instrumentation | Add `source` (VARCHAR) + `source_meta` (JSONB) to `conversation_sessions`; SQL queries against the new columns |

### Visual constraints (per project memory)
- **No gradients anywhere.** Solid colors only.
- **Brand color discipline.** Use `var(--primary)` (`#312e81` indigo) deliberately. Existing tokens `--secondary` (amber `#f59e0b`) and `--success` (emerald `#059669`) are allowed where they already appear; don't introduce new accent palettes.

---

## Architecture overview

```
┌──────────────────────────────┐    ┌──────────────────────────────┐
│   luran-dashboard (Next.js)  │    │   luran-chat (Next.js)       │
│                              │    │                              │
│  /dashboard/settings/company │    │  /app/explore  (auth)        │
│   ├ category dropdown        │    │   ├ personal block           │
│   └ public listing toggle    │    │   └ browse grid              │
│                              │    │                              │
│  /dashboard (home)           │    │  /businesses (public)        │
│   └ category-prompt banner   │    │   └ browse grid only         │
└──────────────┬───────────────┘    └──────────────┬───────────────┘
               │                                   │
               │  PATCH /api/v1/tenants/me         │  GET /api/v1/businesses           (extended)
               │   (existing — accepts new field)  │  GET /api/v1/customer/            (new)
               │                                   │       personal-businesses
               ▼                                   ▼
┌─────────────────────────────────────────────────────────────────┐
│                    luran (FastAPI backend)                      │
│                                                                 │
│  app/api/v1/endpoints/public.py            ← extend list_public_…       │
│  app/api/v1/endpoints/customer_explore.py  ← new /personal-businesses   │
│  app/services/visitor_service.py           ← extend list query          │
│  app/services/customer_explore_service.py  ← new personal-block query   │
│                                                                 │
│  app/db/models/tenant.py          ← + category column           │
│  app/db/models/conversation.py    ← + source, source_meta       │
│  alembic/versions/<new>_…         ← migration                   │
│                                                                 │
│  app/schemas/visitor.py           ← + category fields           │
│  app/schemas/customer_explore.py  ← new                          │
└─────────────────────────────────────────────────────────────────┘
                                │
                                ▼
                        Postgres (single DB)
```

**What this design intentionally avoids:** new microservices, queues, caches, real-time pub/sub, third-party SDKs, geographic indexing, denormalization (yet).

---

## Data model

### `tenants` — new column

| Column | Type | Null | Default | Notes |
|---|---|---|---|---|
| `category` | `VARCHAR(40)` | yes | NULL | App-validated against `TenantCategory` enum |

Index:
```sql
CREATE INDEX CONCURRENTLY ix_tenants_listed_category
  ON tenants (is_publicly_listed, category)
  WHERE is_publicly_listed = true;
```
Partial index — the browse grid's WHERE is *always* `is_publicly_listed = true [AND category = ?]`.

### `conversation_sessions` — new columns

| Column | Type | Null | Default | Notes |
|---|---|---|---|---|
| `source` | `VARCHAR(32)` | yes | NULL | App-validated against `ConversationSource` enum |
| `source_meta` | `JSONB` | yes | NULL | Optional attribution context |

Index:
```sql
CREATE INDEX CONCURRENTLY ix_conversation_sessions_source_created
  ON conversation_sessions (source, created_at);
```

### Convention departure: VARCHAR + Pydantic enum vs. `SQLEnum`

The codebase convention is `SQLEnum` (Postgres native ENUM). For these two new columns, we deliberately use `VARCHAR + Pydantic enum validation` because they are UX-facing taxonomies that will evolve. Adding/renaming a category should not require an `ALTER TYPE` migration.

Trade-off accepted: lose DB-level enforcement; rely on the Pydantic boundary. Audit logs don't change.

### Enum definitions

```python
# app/schemas/explore.py (or similar)
from enum import Enum

class TenantCategory(str, Enum):
    RETAIL_ECOMMERCE         = "retail_ecommerce"
    SOFTWARE_SAAS            = "software_saas"
    FOOD_BEVERAGE            = "food_beverage"
    HEALTH_WELLNESS          = "health_wellness"
    BEAUTY_PERSONAL_CARE     = "beauty_personal_care"
    EDUCATION_COACHING       = "education_coaching"
    PROFESSIONAL_SERVICES    = "professional_services"
    FINANCIAL_INSURANCE      = "financial_insurance"
    TRAVEL_HOSPITALITY       = "travel_hospitality"
    REAL_ESTATE              = "real_estate"
    MEDIA_ARTS_ENTERTAINMENT = "media_arts_entertainment"
    NONPROFIT                = "nonprofit"
    OTHER                    = "other"

class ConversationSource(str, Enum):
    WIDGET           = "widget"
    EXPLORE_PERSONAL = "explore_personal"
    EXPLORE_BROWSE   = "explore_browse"
    EXPLORE_SEARCH   = "explore_search"
    DIRECT           = "direct"
    UNKNOWN          = "unknown"
```

Slugs are `snake_case`, never user-displayed. Human labels live in TS frontends (see Frontend section).

### Migration notes

- Single Alembic revision: `2026_05_07_explore_phase_a.py`
- Down-migration drops the three columns + two indexes — fully reversible
- No data backfill — existing rows have NULL `category` / NULL `source`, which is the intended steady state for legacy data
- Online-safe (Postgres ≥ 11, we are 14+); use `CREATE INDEX CONCURRENTLY` in the migration
- Flag for `database-ops-engineer` review before prod apply

---

## API contract

### `GET /api/v1/businesses` (extended)

Anonymous-allowed. **No breaking change** for existing callers.

**Query params**

| Name | Type | Default | Notes |
|---|---|---|---|
| `q` | `str?` | none | existing — name/headline search, max 120 chars |
| `page` | `int` | 1 | existing |
| `page_size` | `int` | 24 | existing |
| `category` | `TenantCategory?` | none | **new** |

**Sort:** `is_featured DESC, (created_at >= now() - interval '30 days') DESC, display_name ASC`

**Response**

```jsonc
{
  "businesses": [
    {
      "public_slug": "starbucks-ng",
      "display_name": "Starbucks NG",
      "description": "...",
      "support_headline": "...",
      "support_subheadline": "...",
      "widget_logo_url": "...",
      "is_featured": true,
      "category": "food_beverage",      // NEW — may be null
      "created_at": "2026-04-22T..."     // NEW — used by frontend for "New" badge
    }
  ],
  "total": 42,
  "total_pages": 4
}
```

No `category_counts` in Phase A — chips render without counts.

### `GET /api/v1/customer/personal-businesses` (new)

Auth required (customer JWT). 401 if no auth, 403 for non-customer principals.

**No query params.** The endpoint is contextual to the authenticated customer.

**Response**

```jsonc
{
  "businesses": [
    {
      "public_slug": "starbucks-ng",
      "display_name": "Starbucks NG",
      "description": "...",
      "support_headline": "...",
      "support_subheadline": "...",
      "widget_logo_url": "...",
      "is_featured": true,
      "category": "food_beverage",
      "created_at": "2026-04-22T...",

      "relationship": {
        "is_followed": true,
        "followed_at": "2026-04-12T09:30:00Z",
        "last_chat_at": "2026-05-05T16:14:00Z",
        "last_activity_at": "2026-05-05T16:14:00Z"
      }
    }
  ]
}
```

**Server-enforced limits**
- Max 8 items
- Only `is_publicly_listed = true` tenants
- 30-day cutoff applies to the recent-chat tail; follows are not time-bound

**Caching:** `Cache-Control: private, max-age=120, stale-while-revalidate=300`

### `PATCH /api/v1/tenants/me` (extended)

Existing endpoint accepts new optional `category` field.

```jsonc
{
  "category": "food_beverage"   // or null to clear
}
```

Validation: must be a `TenantCategory` member or null. Auth: existing scope.

### Conversation creation path (extended)

Wherever `ConversationSession` is created (visitor bootstrap and customer chat-start), accepts:

```jsonc
{
  "source": "explore_browse",
  "source_meta": {
    "category_filter": "food_beverage",
    "search_query_present": false,
    "position": 4,
    "page": 1
  }
}
```

Both optional. Server defaults `source` to `"unknown"` if absent. Unknown `source_meta` keys silently dropped (forward-compat) with a counter.

### Surface → attribution mapping

| Surface | `source` | `source_meta` |
|---|---|---|
| Embedded widget on a tenant's site | `widget` | (none) |
| Personal-block card click → start chat | `explore_personal` | `{ "position": N }` |
| Browse-grid card click → start chat | `explore_browse` | `{ "category_filter": "..."?, "position": N, "page": M }` |
| Search-result card click → start chat | `explore_search` | `{ "search_query_present": true, "position": N }` |
| Direct `/b/<slug>` link | `direct` | (none) |

---

## Backend service layer

### Module placement

| What | Where |
|---|---|
| Browse query (extended) | `app/services/visitor_service.py::list_public_businesses` |
| Personal-block query (new) | `app/services/customer_explore_service.py::get_personal_businesses` (new file, matches the naming pattern of `customer_engagement.py`, `customer_conversations.py`) |
| Tenant category mutation | `app/services/tenant_service.py` (existing path; allow new field) |
| Conversation source attribution | wherever `ConversationSession` is created — write straight onto the model |

`customer_explore_service.py` is a new file, sitting alongside the existing `customer_engagement.py` etc. — matches the codebase's per-feature naming convention.

### Browse query (extended)

```sql
SELECT
  id, public_slug, display_name, description,
  support_headline, support_subheadline, widget_logo_url,
  is_featured, category, created_at
FROM tenants
WHERE is_publicly_listed = true
  AND (:category IS NULL OR category = :category)
  AND (
    :q IS NULL
    OR display_name ILIKE '%' || :q || '%'
    OR support_headline ILIKE '%' || :q || '%'
  )
ORDER BY
  is_featured DESC,
  (created_at >= now() - interval '30 days') DESC,
  display_name ASC
LIMIT :page_size OFFSET :offset;
```

Companion count query mirrors the WHERE.

**ILIKE caveat:** `ILIKE '%foo%'` does not use B-tree indexes. Acceptable at current scale. If search hot-spots in production, promote to `pg_trgm` GIN index in Phase B.

### Personal-block query (new)

```sql
WITH followed AS (
    SELECT
        tenant_id,
        created_at AS followed_at,
        last_viewed_channel_at
    FROM customer_business_follows
    WHERE customer_user_id = :customer_id
),
recent_chats AS (
    SELECT
        tenant_id,
        MAX(last_message_at) AS last_chat_at
    FROM conversation_sessions
    WHERE customer_user_id = :customer_id
      AND last_message_at >= now() - interval '30 days'
    GROUP BY tenant_id
),
combined AS (
    SELECT
        COALESCE(f.tenant_id, r.tenant_id) AS tenant_id,
        f.followed_at,
        f.last_viewed_channel_at,
        r.last_chat_at,
        (f.tenant_id IS NOT NULL) AS is_followed,
        GREATEST(
            COALESCE(f.followed_at,             '-infinity'::timestamptz),
            COALESCE(f.last_viewed_channel_at,  '-infinity'::timestamptz),
            COALESCE(r.last_chat_at,            '-infinity'::timestamptz)
        ) AS last_activity_at
    FROM followed f
    FULL OUTER JOIN recent_chats r USING (tenant_id)
)
SELECT
    t.id, t.public_slug, t.display_name, t.description,
    t.support_headline, t.support_subheadline, t.widget_logo_url,
    t.is_featured, t.category, t.created_at,
    c.is_followed, c.followed_at, c.last_chat_at, c.last_activity_at
FROM combined c
JOIN tenants t ON t.id = c.tenant_id
WHERE t.is_publicly_listed = true
ORDER BY c.last_activity_at DESC
LIMIT 8;
```

Indexes used (all already exist):
- `customer_business_follows (customer_user_id, created_at DESC)` — for the `followed` CTE
- `conversation_sessions (customer_user_id, last_message_at)` — for the `recent_chats` CTE
- `tenants` PK on `id` — for the final join

Expected runtime: single-digit ms for typical customers (≤100 follows, ≤500 conversations).

### Service-method signatures

```python
class VisitorService:
    def list_public_businesses(
        self,
        *,
        query: str | None = None,
        page: int = 1,
        page_size: int = 24,
        category: TenantCategory | None = None,   # NEW
    ) -> PublicBusinessDirectoryResponse: ...

class CustomerExploreService:
    def get_personal_businesses(
        self,
        *,
        customer_user_id: UUID,
    ) -> PersonalBusinessesResponse: ...
```

### Implementation notes

- Express CTEs via SQLAlchemy 2.0 Core (`select(...).cte(...)`). Avoid raw `text()` except for `'-infinity'::timestamptz` literals
- Service methods are pure-read; no transactions to manage
- N+1 protection: trivial — at most 2 SQL statements per service call (count + page for browse; one CTE-driven SELECT for personal)

### Deliberate non-goals

- Not denormalizing `last_activity_at` onto `customer_business_follows`
- Not adding a service-layer cache (HTTP `Cache-Control` is sufficient)
- Not using arq/background workers
- Not building a "discover similar businesses" recommender

---

## Customer frontend (`luran-chat`)

### Component breakdown

```
app/components/customer/
├── BusinessDirectoryPage.tsx       (existing — refactored to orchestrator)
├── BusinessCard.tsx                (NEW — extracted from inline markup)
├── PersonalBusinessesBlock.tsx     (NEW)
├── CategoryFilterChips.tsx         (NEW)
└── hooks/
    ├── useBrowseBusinesses.ts      (NEW)
    └── usePersonalBusinesses.ts    (NEW)
```

### Page wiring

`/app/explore/page.tsx` — auth-gated. SSR-fetches both endpoints in parallel, passes initial data to `BusinessDirectoryPage` with `showPersonalBlock`.

`/businesses/page.tsx` — public. SSR-fetches browse only, omits `showPersonalBlock`.

### URL-state-driven filtering (refactor)

The existing component keeps `search`, `query`, `page` in `useState` — the URL doesn't reflect the filtered view. Phase A switches to `useSearchParams` + `router.replace(url, { scroll: false })`:

| URL param | Drives |
|---|---|
| `?q=` | search box |
| `?category=` | selected chip + `category` API filter |
| `?page=` | pagination |

`/app/explore?category=food_beverage&page=2&q=lagos` becomes a shareable, refresh-safe state.

### Data fetching

Two React Query hooks (verify SDK at impl time):

```ts
function useBrowseBusinesses(initial, params) {
  return useQuery({
    queryKey: ["browse-businesses", params],
    queryFn: () => fetchPublicBusinesses(params),
    initialData: shouldUseInitial(params) ? initial : undefined,
    staleTime: 30_000,
  });
}

function usePersonalBusinesses(initial) {
  return useQuery({
    queryKey: ["personal-businesses"],
    queryFn: fetchPersonalBusinesses,
    initialData: initial,
    staleTime: 2 * 60_000,   // matches Cache-Control: max-age=120
  });
}
```

Personal block must be invalidated whenever the user's relationships change. Audit the existing follow/unfollow and chat-start mutation hooks during implementation; if they don't already invalidate `["personal-businesses"]`, add `queryClient.invalidateQueries({ queryKey: ["personal-businesses"] })` to their `onSuccess` paths.

### Personal block UI

- 4-col grid on desktop, 2 on tablet, 1 on mobile
- 3px left-edge accent in `var(--primary)` (matches existing browse-card hover state)
- "Followed" pill: solid `var(--primary)` bg, white text — **no gradient**
- Last-activity label: `"Chatted 2d ago"` / `"Followed Apr 12"` — choose by which timestamp drives `last_activity_at`
- Section header: small uppercase eyebrow, "YOUR BUSINESSES" + sub-caption "Picked up where you left off"
- Hidden entirely if `businesses.length === 0` OR if active search yields zero matches client-side

### Category filter chips

- Horizontal flex container, `overflow-x-auto`, snap-x on mobile
- Idle: `bg-surface` + `border border-border-subtle`, dark text
- Selected: solid `var(--primary)` bg, white text — **no gradient**
- Hover (idle): `bg-foreground/5`
- Click selected → clears (URL `?category=` removed)
- Click idle → sets, scrolls grid into view (`scrollIntoView({ behavior: 'smooth', block: 'nearest' })`)
- No counts on chips (Phase A)

### Search behavior across both surfaces

- Server-side: applies to browse grid only (existing behavior + new category filter)
- Client-side: applies to personal block — array.filter on `display_name` + `support_headline` over the (≤8) cached items
- When `q` is non-empty AND personal-filtered length is 0 → personal block silently hides
- Debounce 250ms (existing)

### Conversation-start attribution

Each card's onClick (or wherever conversation creation fires) calls a small utility:

```ts
function attributionFromExploreClick(opts: {
  surface: 'personal' | 'browse' | 'search';
  category?: TenantCategory;
  position: number;
  page?: number;
}): { source: ConversationSource; source_meta: SourceMeta } {
  return {
    source: `explore_${opts.surface}` as ConversationSource,
    source_meta: {
      category_filter: opts.category ?? null,
      search_query_present: opts.surface === 'search',
      position: opts.position,
      page: opts.page ?? 1,
    },
  };
}
```

### Frontend non-goals

- No design-system library introduction
- No virtualization of the browse grid
- No infinite scroll
- No skeleton-loading redesign — match existing `Loader2` pattern

---

## Dashboard frontend (`luran-dashboard`)

### Category dropdown — `/dashboard/settings/company`

Schema extension (`react-hook-form` + zod):

```ts
category: z.enum([
  "retail_ecommerce", "software_saas", "food_beverage",
  "health_wellness", "beauty_personal_care", "education_coaching",
  "professional_services", "financial_insurance", "travel_hospitality",
  "real_estate", "media_arts_entertainment", "nonprofit", "other",
] as const).nullable().optional(),
```

Form field: a `<Select>` between `description` and `long_description`, wrapped in a div with `id="category" scroll-mt-24` (anchor target for the banner CTA).

Empty string ↔ null on submit (existing form already does this for `public_slug`).

### Banner — `/dashboard` home

Visibility logic:

```ts
const NUDGE_KEY = "luran:nudge:set-category:dismissed";
const showBanner =
  Boolean(tenant) &&
  tenant.is_publicly_listed &&
  !tenant.category &&
  typeof window !== "undefined" &&
  localStorage.getItem(NUDGE_KEY) !== "1";
```

Markup uses solid colors only — `bg-foreground/[0.03]` panel, solid `var(--primary)` icon tile + CTA button. `<X>` dismiss writes `"1"` to localStorage and triggers a re-render.

CTA hash-link: `/dashboard/settings/company#category` — Next.js handles the scroll-to-anchor natively because the field wrapper has `id="category"`.

### Shared label map

```ts
// app/lib/categories.ts (luran-dashboard) — and identical copy in luran-chat
export const CATEGORY_VALUES = [
  "retail_ecommerce", "software_saas", "food_beverage",
  "health_wellness", "beauty_personal_care", "education_coaching",
  "professional_services", "financial_insurance", "travel_hospitality",
  "real_estate", "media_arts_entertainment", "nonprofit", "other",
] as const;

export type TenantCategory = (typeof CATEGORY_VALUES)[number];

export const CATEGORY_LABELS: Record<TenantCategory, string> = {
  retail_ecommerce:         "Retail & E-commerce",
  software_saas:            "Software & SaaS",
  food_beverage:            "Food & Beverage",
  health_wellness:          "Health & Wellness",
  beauty_personal_care:     "Beauty & Personal Care",
  education_coaching:       "Education & Coaching",
  professional_services:    "Professional Services",
  financial_insurance:      "Financial & Insurance",
  travel_hospitality:       "Travel & Hospitality",
  real_estate:              "Real Estate",
  media_arts_entertainment: "Media, Arts & Entertainment",
  nonprofit:                "Non-profit",
  other:                    "Other",
};
```

Duplicated across both apps. Promote to a shared package only when the next shared constant lands.

---

## Error handling

### Backend

| Path | Failure | Response |
|---|---|---|
| `GET /api/v1/businesses?category=invalid` | unknown enum | `422` |
| `GET /api/v1/customer/personal-businesses` | no auth | `401` |
| `GET /api/v1/customer/personal-businesses` | wrong principal type | `403` |
| Either endpoint | DB unreachable | `503` |
| `PATCH /api/v1/tenants/me` invalid `category` | unknown enum | `422` |
| Conversation create | unknown `source` enum | **Coerce silently to `"unknown"`** + counter increment. Conversation creation must not fail because of a new analytics-only field; older clients (e.g., embedded widget) may not know the latest `ConversationSource` values. |
| Conversation create | unknown `source_meta` keys | silently dropped, counter incremented |

### Customer frontend (`/app/explore`)

| Failure | Behavior |
|---|---|
| Personal fetch fails, browse succeeds | Render browse only; log error, **no toast** |
| Browse fetch fails, personal succeeds | Render personal block + existing "Directory unavailable" error card |
| Both fail | Existing error card across full page |
| Personal returns 401 (auth expired) | Treat as empty; auth-refresh interceptor handles re-login |

### Dashboard frontend

| Failure | Behavior |
|---|---|
| `PATCH /tenants/me` with category fails | Existing form-error toast pattern; field stays editable |
| Banner shown but tenant data fails to load | Banner doesn't render — `showBanner` requires resolved tenant |

---

## Edge cases

| Case | Handling |
|---|---|
| Customer follows then unfollows immediately | Personal cache invalidated by mutation; next read returns new state |
| Customer chats with a tenant that later un-lists | `is_publicly_listed = true` filter drops them silently from personal block |
| Customer has 0 follows + 0 recent chats | Personal block hides cleanly |
| Customer has 200 follows, all listed | Server caps at 8; user has `/app/following` for the full set |
| Tenant changes category | Next fetch picks it up; no FK or session_meta tied to the value |
| Tenant unsets `is_publicly_listed` mid-search | Disappears from next browse refetch; existing results may show it stale until paged. Acceptable. |
| Two customers share a browser | localStorage nudge dismissal is per-browser; acceptable for B2B operators |
| Banner dismissed → un-categorize → revisit | Stays hidden (key persists). Rare and benign. |
| Personal-block tenant has `category = NULL` | Renders without category pill — no special-case needed |
| Search yields 0 in both surfaces | Existing "no results" empty card; personal block hidden by `q`-empties-personal rule |
| Same business in followed AND recent chat | One row (FULL OUTER JOIN dedup); `is_followed: true`, `last_activity_at = max(...)` |
| Card click race condition | Attribution computed at click time from surface, not from cache state — works correctly |
| 30-day cutoff timezone edge | `now() - interval '30 days'` evaluates in DB UTC. Boundary day acceptable. |

---

## Testing strategy

### Backend (`luran/`)

**Unit (no DB):**
- All 13 `TenantCategory` values round-trip through Pydantic
- All 6 `ConversationSource` values
- `source_meta` Pydantic model — accepts documented keys, drops unknowns, type-checks values

**Service (pytest + per-test transaction rollback):**
- `VisitorService.list_public_businesses` — sort order, category filter, q filter, combined, pagination, count == page
- `CustomerService.get_personal_businesses` — follow only, recent only, both overlap, un-listed filter, 30-day cutoff edge, 8-cap, sort by `last_activity_at`, empty result

**API (pytest + httpx):**
- `GET /api/v1/businesses?category=invalid` → 422
- `GET /api/v1/customer/personal-businesses` no auth → 401
- `GET /api/v1/customer/personal-businesses` admin JWT → 403
- `PATCH /api/v1/tenants/me` sets and clears category
- Conversation creation persists `source` + `source_meta`

**Migration:** apply up + down on a populated snapshot, assert structural correctness and data integrity.

### Customer frontend (`luran-chat`)

**Component (Vitest + RTL):**
- `PersonalBusinessesBlock` rendering, "Followed" pill, hide-when-empty, hide-on-search
- `CategoryFilterChips` URL ping-pong stability
- `BusinessCard` with full and partial data
- URL state parsed correctly on mount

**Hooks:** `usePersonalBusinesses` honors initialData, invalidates on mutation; `useBrowseBusinesses` query key includes filters.

**Playwright E2E:**
- Auth'd customer with seeded follows → personal block visible → click chip → URL updates → reload → state restored
- Anonymous on `/businesses` → no personal block → category filter works
- Search flows: known business in personal block matches; unknown query hides personal block

### Dashboard frontend (`luran-dashboard`)

**Component:** schema accept/reject, banner visibility logic, dismissal effect.

**E2E:** toggle `is_publicly_listed` on without category → banner appears → CTA scrolls to field → select Retail → submit → banner gone.

### Performance

One-time `EXPLAIN ANALYZE` pass on both queries with realistic seed data (~10K tenants, ~100K follows, ~500K conversation_sessions). Confirm both <50ms p95.

---

## Rollout plan

Single coordinated deploy. Backwards-compat-safe — no flags, no dark-launching.

### Order

1. **Backend (Alembic migration + service code)** — first. Migration adds nullable columns + indexes (online-safe). `CREATE INDEX CONCURRENTLY` in production.
2. **Dashboard** — second. Tenants can start setting categories.
3. **Customer app (`luran-chat`)** — last. Once at least *some* tenants have categorized themselves so chips aren't empty.

### Why this order is safe

- Old dashboard + old customer-app continue to work against the new backend (just don't send `category`/`source`)
- New dashboard + new customer-app require backend ≥ Phase A
- The personal-businesses endpoint is the only truly new surface; it sits unused until customer-app calls it

### Monitoring (week 1)

- p95 latency on `GET /api/v1/customer/personal-businesses` — target <50ms; alarm >200ms
- p95 latency on `GET /api/v1/businesses` — alarm if regresses 20%+
- Error rate on both endpoints — target <0.1%
- DB plan stability via `pg_stat_statements`
- Daily snapshot: `% of listed tenants with category set`

### Phase B trigger conditions

| Trigger | Indicates | Phase B move |
|---|---|---|
| <5% of conversations from `explore_*` after 4 weeks | Discovery surface isn't moving the needle | Re-evaluate; consider trending or recommendations |
| Category set-rate >40% within 30 days | Taxonomy is sticking | Add chip counts; consider multi-category |
| Personal-block query p95 >50ms | Indexed reads aren't enough | Denormalize `last_activity_at` onto `customer_business_follows` |
| Search latency complaints | ILIKE scan is hurting | Add `pg_trgm` GIN index |

### Doc deliverables (out of code)

- API doc update for new endpoint + new fields
- One-paragraph note in dashboard help section explaining what `category` does
- README note on `luran-chat` describing the URL-state contract for `/app/explore`

---

## Implementation handoff

Phase A spans three repos. Suggested agent allocation:

| Phase | Scope | Agent |
|---|---|---|
| Migration + backend service + endpoints | `luran/` | `backend-engineer` (with `database-ops-engineer` review on the migration) |
| Dashboard category field + nudge banner | `luran-dashboard/` | `fullstack-nextjs` |
| `/app/explore` redesign + URL state + attribution wiring | `luran-chat/` | `fullstack-nextjs` |

Coordination point: backend ships first; the two frontends can implement in parallel afterwards.

---

## Open items deliberately deferred

- Geographic data (the original "businesses near you" idea — explicitly out of scope per strategic decision)
- Multi-category support
- Follow-velocity / chat-velocity ranking
- PostHog or third-party analytics SDK
- Category-management CMS for platform admins
- Real-time invalidation of the personal block
- Sponsored placement / monetization on browse grid
- Server-side preference store for dismissed nudges
- Personal-block "see all" CTA
