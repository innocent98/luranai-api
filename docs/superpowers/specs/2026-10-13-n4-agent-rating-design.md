# N4 — Agent-requested rating & review (design)

**Status:** DRAFT, written overnight 2026-10-13 under Adebayo's delegation. Every decision below is a **controller ruling Adebayo can overturn**; §11 lists the ones most worth his review.
**Package:** after screen recording (handoff order: screen recording → **N4** → presence → N2).
**Repos:** `luran` (BE, feature → `develop`), `luran-chat` (feature → `stage`), `luran-dashboard` (feature → `staging`), `luran_agent` (feature → `main`). `luran-widget` (embed loader) needs no change.
**Related:** context sweep `docs/handoff/n4-context-2026-10-13.md` (workspace); triage `docs/handoff/2026-10-10-observations-triage.md` §"Feature N4" (workspace); N1 missed-lifecycle SOP `docs/sop/missed_ticket_lifecycle.md`; screen-recording design `2026-10-12-screen-recording-design.md` (error-shape and spec conventions reused).
**PRD:** `docs/luran_prd.md` Feature 5.1 (Conversation Analytics) lists **"CSAT Score — if feedback collection is enabled"** as a core metric. The PRD has no rating feature beyond that. N4 is new scope from field feedback (triage 2026-10-10). It makes CSAT measurable **per human agent**, which is the part Feature 5.1 cannot answer today.

## 1. Problem

A customer rating system already exists end to end: `session_ratings` (score 1–5 + comment), public `POST /reviews/session`, tenant `GET /reviews/session`, the dashboard ratings page, and the widget's emoji `RatingPrompt`. But:

- **No attribution.** A rating belongs to a session, not to whoever helped. When an agent resolves a ticket, the widget already shows the generic prompt (`ticket_resolved` → `triggerRatingForResolution('agent_resolved')`). That rating lands with no agent on it. Admins cannot tell how individual agents are doing.
- **One rating per session.** The constraint `uq_session_rating_per_session` plus the unique index `ix_session_ratings_session_id` (migration `6d7e9f0a1b32`) allow only one rating per session. A session the AI started and a human finished cannot carry both an AI rating and a rating for the agent.
- **No way to ask.** Agents cannot ask for feedback, and they cannot see their own feedback.
- **Weak trust model.** The public POST only checks `user_identifier`. Anyone who holds the session id and visitor id (staff included) can post a rating. If ratings carry an agent's name, that is not good enough.

## 2. Goals / non-goals

**Goals**
- A human agent (or an admin) can **request a rating** from the dashboard ticket chat and the app takeover. A rating is also requested **automatically when the ticket is resolved** with a human assignee.
- The customer sees **"How was your chat with {first name}?"** with the existing emoji 1–5 card (+ optional comment) in the widget, the hosted support page and the logged-in views. The rating is recorded **against that agent**.
- **Admins** see per-agent CSAT/average/count, can filter ratings (agent, AI vs human, date), and see ratings on ticket and conversation detail.
- **Agents** see **their own** ratings in the app ("My ratings"), and never anyone else's.
- The existing AI/idle rating keeps working unchanged for old and new clients.

**Non-goals (v1):** per-rating push/email notifications; AI moderation of comments; agents replying to ratings; public/display of ratings to customers; multi-question surveys / NPS; ratings for the auto-resolver path; a tenant on/off toggle (see §11 Q6); ratings via voice; a CSAT tile on the main analytics overview (follow-up, §12).

## 3. Decisions (controller rulings, 2026-10-13)

| ID | Ruling |
|---|---|
| N4-D1 | **Extend `session_ratings`, no parallel table.** New columns `rated_agent_id` (nullable FK `users`; NULL = AI / no human), `source` and `rating_request_id` (nullable FK). Uniqueness = **one rating per (session, rated party)** through two partial unique indexes: `(session_id, rated_agent_id) WHERE rated_agent_id IS NOT NULL` and `(session_id) WHERE rated_agent_id IS NULL`. Existing rows keep `rated_agent_id` NULL. **Refined:** `source` gets a 4th value, `resolution_prompt`, because the code proves that untokened post-resolve ratings exist today (`meta.source` = `agent_resolved`/`ticket_closed`) and will keep arriving (old widgets; tickets with no human). Values: `idle_prompt` \| `resolution_prompt` \| `manual_request` \| `auto_resolve`. |
| N4-D2 | **New `rating_requests` table** (§4.1): `tenant_id`, `session_id`, `ticket_id?`, `agent_id` (snapshot of `Ticket.assigned_to`), `requested_by?` (NULL for auto), `source`, `token_hash` (random token, stored SHA-256), `message_id?`, `created_at`, `expires_at` (+7 days), `fulfilled_at?`. A rating with a human `rated_agent_id` **requires** a valid, unfulfilled, unexpired request token for that session. The client **never sends an agent id**: the server derives `rated_agent_id` from the request. The public POST stays unauthenticated but is token-gated for human ratings. The legacy untokened rating keeps working with `rated_agent_id` NULL. |
| N4-D3 | **Triggers.** (a) **Manual** "Request rating" from the dashboard ticket chat and the app takeover. Allowed for the assigned agent or a tenant admin, only while a human is attached: ticket `in_progress`, or `resolved` with `resolved_at` in the last 24 h, **and** `assigned_to` not NULL. (b) **Auto** inside `POST /tickets/{id}/resolve` when the ticket has a human assignee, no open request exists for (session, agent), and that agent has not been rated in this session yet. (c) The **auto-resolver cron does not request** (customer gone, no actor). (d) Close (`POST /tickets/{id}/close`) does not create a request. **Anti-spam:** at most 1 open request per (session, agent); a **manual** request within **10 min** of the previous request (any source) for (session, agent) → **409 `rating_request_cooldown`** (auto-on-resolve is exempt: it reuses an open request instead). A manual request after the cooldown while one is still open **supersedes** it: the old one is expired, a new token is issued. |
| N4-D4 | **Delivery.** Persist an ASSISTANT message with content "How was your chat with {first name}?" and `meta = {system_notice: "rating_request", request_id, agent_first_name, rating_token}`. Fan out WS `rating_requested` {request_id, token, agent_name, message_id, …} **to the customer only**. Agents get a token-free `rating_request_created`. `ticket_resolved`/`ticket_closed` gain `rating_request_id` + `rating_state` so the widget **replaces** its generic prompt with the bound card (no double prompt). The client "rated" flag becomes **per request id**. **Security tradeoff:** the token sits in the customer's own transcript (message meta, encrypted at rest because `Message.meta` is `EncryptedJSON`) and in the widget's localStorage. It is **redacted from every staff-facing serializer** (§4.6), so agents cannot rate themselves through the dashboard. |
| N4-D5 | **Visibility.** Agents: new **`GET /agent/me/ratings`** (own ratings only; average, count, CSAT %, last 20 with score/comment/date/ticket number; no customer identity). **Refined:** the app router is mounted at `/agent`, not `/agent-app` (`app/api/v1/api.py:279`). Admins: per-agent CSAT/average/count in `GET /analytics/agents` and `GET /analytics/agents/{id}`; ratings-list filters (agent, AI vs human, source, date); the rating on ticket and conversation detail. **Support agents never see other agents' ratings.** Today `GET /reviews/session` returns all tenant ratings to any tenant role, which contradicts this, so it is scoped (§4.3.4). |
| N4-D6 | **CSAT = % of ratings scoring ≥ 4**, plus average and count; `null` (not 0) when count is 0. Default window = last 30 days (the existing `AnalyticsService._get_default_date_range`). **Refined:** the parameters are `start_date`/`end_date`, not `from`/`to`, matching every existing analytics and reviews endpoint. The window filters on the **rating's** `created_at`. |
| N4-D7 | **Comment ≤ 500 chars** (the widget already caps at 500; the BE schema allows 2000 today and is tightened). Stored as is. **Encrypted at rest from now on:** `session_ratings.comment` is plain `Text` today while every other piece of customer chat text (`Message.content`, `Message.meta`) is encrypted, so the column moves to `EncryptedString` and the migration encrypts existing rows (§4.1). No AI moderation in v1. |
| N4-D8 | **No notifications in v1** (no push/email per rating). Live WS frames that keep the UI correct (`rating_request_created`) are not notifications. |
| N4-D9 | **Surfaces:** BE (`luran`); customer FE (`luran-chat`: widget, hosted `/support/[slug]`, the logged-in ChatWindow views and `/messages/[sessionId]`); dashboard (`luran-dashboard`: request button in ticket chat, CSAT columns, filters, ticket/conversation rating badge); agent app (`luran_agent`: request button in takeover, "My ratings" from Settings). **Contract-first:** the BE ships to staging and is live-captured into the FE guide before any FE plan is written. |
| N4-D10 | **Purge semantics (new, forced by the code).** `AccountPurgeService.purge_user` NULLs every inbound FK to `users` (`account_purge_service.py:447`). Doing that to `session_ratings.rated_agent_id` would turn a human rating into an "AI" rating and could hit the NULL partial unique index. So `rated_agent_id` and `rating_requests.agent_id` are **kept** on purge (the user row survives as a PII-free tombstone, so nothing personal remains). `rating_requests.requested_by` **is** NULLed, like other "who did it" columns. FKs use no ON DELETE action (RESTRICT); `purge_tenant` already deletes ratings before users and gains `rating_requests`. |

## 4. Backend (`luran`)

### 4.1 Data model + migration (one Alembic revision after `sr10screenrec`)

**New table `rating_requests`** (`app/db/models/review.py`, model `RatingRequest`):

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | `UUIDMixin` |
| `tenant_id` | FK `tenants` ON DELETE CASCADE, NOT NULL | |
| `session_id` | FK `conversation_sessions` ON DELETE CASCADE, NOT NULL | |
| `ticket_id` | FK `tickets` ON DELETE SET NULL, nullable | the ticket the request came from |
| `agent_id` | FK `users`, NOT NULL, no ON DELETE action | snapshot of `Ticket.assigned_to` at request time (N4-D10) |
| `requested_by` | FK `users` ON DELETE SET NULL, nullable | NULL for `auto_resolve`; NULLed by `purge_user` |
| `source` | varchar(32) + CHECK in (`manual_request`, `auto_resolve`) | |
| `token_hash` | varchar(64), NOT NULL, unique index | `sha256(token).hexdigest()`; token = `secrets.token_urlsafe(32)`; the plaintext is never stored server-side except inside the customer's message meta (encrypted) |
| `message_id` | FK `messages` ON DELETE SET NULL, nullable | the "How was your chat…" message |
| `created_at` | timestamptz NOT NULL | |
| `expires_at` | timestamptz NOT NULL | `created_at + 7 days`; set to `now()` when superseded |
| `fulfilled_at` | timestamptz, nullable | set in the same transaction as the rating insert |

Indexes: `(session_id, agent_id, created_at DESC)` for open-request and cooldown lookups; `(tenant_id, created_at)`.
**"Open"** = `fulfilled_at IS NULL AND expires_at > now()`. This is not expressible as a partial index (`now()` is not immutable), so the 1-open rule is enforced in the service under a `SELECT … FOR UPDATE` on the ticket row (§4.2). That lock also serialises manual-vs-auto races.

**`session_ratings` changes:**

| Column | Type | Notes |
|---|---|---|
| `rated_agent_id` | FK `users`, nullable, no ON DELETE action | NULL = AI / no human |
| `source` | varchar(32) + CHECK in (`idle_prompt`, `resolution_prompt`, `manual_request`, `auto_resolve`), NOT NULL after backfill | |
| `rating_request_id` | FK `rating_requests` ON DELETE SET NULL, nullable, unique | one rating per request |
| `comment` | model type `Text` → `EncryptedString` (DB type stays `text`) | N4-D7 |

- **Drop both** old uniqueness objects: constraint `uq_session_rating_per_session` **and** unique index `ix_session_ratings_session_id`. Recreate the latter as non-unique. The model's `unique=True` on `session_id` and the `UniqueConstraint` are removed.
- Add partial unique indexes `uq_session_ratings_session_agent` `(session_id, rated_agent_id) WHERE rated_agent_id IS NOT NULL` and `uq_session_ratings_session_ai` `(session_id) WHERE rated_agent_id IS NULL`.
- Add index `ix_session_ratings_tenant_agent_created` `(tenant_id, rated_agent_id, created_at)` for the analytics queries.
- The relationship `backref="rating"` (one-to-one) becomes `backref="ratings"` (a list). Grep shows no reader of `session.rating` today.

**Backfill (in the migration):**
1. `source` from the plain-JSON `meta`: `meta->>'source'` = `idle_prompt` → `idle_prompt`; `agent_resolved` / `ticket_closed` → `resolution_prompt`; anything else or missing → `idle_prompt`. Then `SET NOT NULL`.
2. `rated_agent_id` stays NULL for every existing row (N4-D1). **Not** inferring the agent from the ticket assignee: the old prompt asked about "your experience talking to us", not about a named agent. See §11 Q3.
3. **Comment encryption:** iterate rows in batches; for each non-NULL comment, try `decrypt_field`. If it decrypts, it is already encrypted, so skip it (idempotent). Otherwise `encrypt_field` and update. This needs `ENCRYPTION_KEY` in the migration environment, which the API container that runs `alembic upgrade` already has. Downgrade decrypts. The table is small (one row per rated session). Precedent: `scripts/backfill_encrypt_messages.py`, which did the same as a script. Here it lives in the migration, so the model change and the data can never be out of step.

**Downgrade:** drop the new indexes and columns and `rating_requests`; decrypt comments; recreate `uq_session_rating_per_session` + unique `ix_session_ratings_session_id`. This **fails if any session has more than one rating**. That is documented in the SOP, and rows can be de-duplicated (keep the AI row) before downgrading.

### 4.2 Service: `RatingRequestService` (`app/services/rating_request_service.py`)

`create_request(db, ticket, *, source, requested_by) -> RatingRequestResult | None`:
1. `SELECT … FOR UPDATE` the ticket. `agent_id = ticket.assigned_to`; NULL → `no_human_agent`.
2. If a rating exists for (session, agent) → `already_rated`.
3. **Manual only:** latest request for (session, agent) with `created_at > now() - 10 min` → `rating_request_cooldown` (with `retry_after_seconds`). Auto requests skip the cooldown and go to step 4 (reuse); if no open request exists they create one.
4. If an open request exists: `auto_resolve` → **reuse it** (return it, no new message or frame); `manual_request` → supersede (`expires_at = now()`) and continue.
5. Mint the token, insert the `RatingRequest`, then `ConversationService.add_message(role=ASSISTANT, content=f"How was your chat with {first}?", meta={system_notice: "rating_request", request_id, agent_first_name: first, rating_token: token})`. `first` = first whitespace token of `User.full_name`; if that is empty, the copy is "How was your chat with our team?" and `agent_first_name` is null. Store `message_id`. Commit.
6. Return `(request, token, message)`. The caller does the fanout after commit (§4.5).

The message stays in the AI's conversation context like the other `system_notice` messages (`ticket_missed`, `screen_recording_ack`). `add_message` bumps `session.last_message_at`. That is harmless: the auto-resolver keys on `Ticket.updated_at`, not on the session.

### 4.3 Endpoints

**Error shape for every new error:** `{"detail": {"code": "<code>", "message": "<text>"}}`, the object form `/chat/upload` and the video endpoints use. Existing string-detail errors on `POST /reviews/session` (`"Session not found"`, `"User identifier does not match session"`, the legacy 409) are **unchanged**, because old widgets match on `detail === 'Session not found'`. The live guide captures the exact bodies.

#### 4.3.1 `POST /tickets/{ticket_id}/rating-requests` (dashboard + app)
Auth: `get_current_user`; role `tenant_admin` or `support_agent`; same tenant (`verify_tenant_access`).
Body: none.

| Status | Body / code | When |
|---|---|---|
| 201 | `{request_id, ticket_id, session_id, agent_id, agent_name, source: "manual_request", message_id, created_at, expires_at}`, **no token** | created (or superseded and re-created) |
| 404 | `"Ticket not found"` (existing string form) | unknown / other tenant |
| 403 | `rating_request_forbidden` | caller is neither the assignee nor an admin |
| 409 | `no_human_agent` | `assigned_to` is NULL |
| 409 | `ticket_not_ratable` | status not `in_progress`, or `resolved` > 24 h ago, or `open`/`missed`/`closed` |
| 409 | `already_rated` | the customer already rated this agent in this session |
| 409 | `rating_request_cooldown` + `retry_after_seconds` | < 10 min since the last request for (session, agent) |

#### 4.3.2 `GET /tickets/{ticket_id}/rating-status` (dashboard + app; drives the button and the ticket badge)
Same auth as above. Support agents may read it for any ticket they can open today (it shows only that ticket's own rating, which every staff member on that ticket already sees in context). **Exception:** the `rating` field is returned only to admins and to the rated agent; other support agents get `rating: null`, per N4-D5.
200 → `{can_request: bool, reason_code: null | "no_human_agent" | "ticket_not_ratable" | "already_rated" | "rating_request_cooldown" | "rating_request_forbidden", retry_after_seconds: int | null, open_request: {request_id, source, created_at, expires_at} | null, rating: {id, score, comment, created_at, rated_agent_id, rated_agent_name, source} | null}`.

#### 4.3.3 `POST /reviews/session` (public, extended)
New optional body field `rating_request_token: str`. Any `rated_agent_id` the client sends is **ignored** (Pydantic default drops unknown fields; a test asserts this). `comment` max length 2000 → **500**. New IP rate limiter `http_rating`, **20 / hour per IP** (in `app/utils/http_rate_limiter.py`) → 429 `rating_rate_limited`.

- **Without a token (legacy path):** unchanged checks (session exists, `user_identifier` matches). Insert with `rated_agent_id` NULL. `source` comes from `meta.source` using the backfill mapping (`agent_resolved`/`ticket_closed` → `resolution_prompt`, else `idle_prompt`). Duplicate → existing **409 string** response.
- **With a token:** session + `user_identifier` checks first (same strings). Then look up by `token_hash`:
  - not found, or `request.session_id != body.session_id` → **400 `rating_request_invalid`**
  - `fulfilled_at` set or a rating exists for (session, agent) → **409 `already_rated`**
  - `expires_at <= now()` (includes superseded) → **410 `rating_request_expired`**
  - otherwise insert the rating with `rated_agent_id = request.agent_id`, `source = request.source`, `rating_request_id`, and set `fulfilled_at` in the same transaction. A unique-index race → 409 `already_rated`.
- 201 response: today's `SessionRatingResponse` + `rated_agent_id`, `rated_party` (`"human"`|`"ai"`), `source`, `rating_request_id`.

#### 4.3.4 `GET /reviews/session` (tenant, extended + scoped)
New query params: `rated_agent_id`, `rated_party` (`ai`|`human`), `source`, existing `start_date`/`end_date`/score/pagination.
**Scoping change:** `support_agent` callers are **forced to `rated_agent_id = self`** (any other `rated_agent_id` → 403 `rating_scope_forbidden`; `rated_party=ai` → an empty list). Tenant admins are unscoped within the tenant; platform admins unchanged.
Each item gains `rated_agent_id`, `rated_agent_name`, `rated_party`, `source`, `rating_request_id`, `ticket_id`, `ticket_number` (via the request; null for untokened ratings). The list response gains `csat_percent` (over the same filters) beside `average_score` and `total`. Platform `/ratings/all` gains the same item fields (read-only; no scoping change).

#### 4.3.5 `GET /agent/me/ratings` (app)
Auth: `get_current_agent`. Params: `start_date`, `end_date` (default last 30 days). **Not** gated by `check_analytics_permission`: an agent's own feedback is not a plan analytics feature, and `/reviews/session` is ungated today.
200 → `{period_start, period_end, count, average_score: float|null, csat_percent: float|null, recent: [{id, score, comment, created_at, ticket_number|null, source}]}`. `recent` = the newest 20 in the window. No `user_identifier`, visitor name or session id.

#### 4.3.6 Analytics (admin)
- `GET /analytics/agents` → each `AgentSummaryResponse` gains `ratings_count: int`, `average_rating: float|null`, `csat_percent: float|null`, computed over `start_date`/`end_date` (default 30 days). **FE trap:** the existing ticket fields on this row are all-time (`total_tickets_resolved`) or current; only the rating fields are windowed. The guide calls this out.
- `GET /analytics/agents/{agent_id}` → `AgentPerformanceResponse` gains the same three fields (window = `period_start`/`period_end`). This endpoint already lets an agent read their own metrics, which stays consistent with N4-D5.
- One grouped query, not per-agent (the existing loop is N+1):
  `SELECT rated_agent_id, count(*), avg(score), 100.0 * avg((score >= 4)::int) FROM session_ratings WHERE tenant_id = :t AND rated_agent_id IS NOT NULL AND created_at >= :s AND created_at < :e GROUP BY rated_agent_id`.
  Rounding: average 2 dp, CSAT 1 dp.

#### 4.3.7 Conversation detail
`GET /chat/sessions/{session_id}` (`SessionDetailResponse`) gains `ratings: [{id, score, comment, created_at, rated_party, rated_agent_id, rated_agent_name, source}]`. This lists every rating for the session (AI and human), oldest first; it is admin-only already.

### 4.4 Resolve hook (`tickets.py` `resolve_ticket`, ~:243–311)
After `ticket_service.resolve_ticket` commits:
1. If `resolved_ticket.assigned_to` is set: call `create_request(source="auto_resolve", requested_by=None)` inside `try/except`, with its **own** rollback. A failure is logged (ids only, never the token) and **never fails the resolve**.
2. If a request was created or reused: fan out `rating_requested` (customer) and `rating_request_created` (agents) **before** `ticket_resolved`. The widget clears its session on `ticket_resolved`, so the bound card must already be in state.
3. `ticket_resolved` gains `rating_request_id` (str|null) and `rating_state`: `"pending"` (an open request exists), `"already_rated"` (this agent already rated in this session), or `"none"` (no human assignee, so the generic prompt is fine).
4. `TicketResponse` from resolve gains optional `rating_request_id`.
An admin resolving an unassigned MISSED ticket → `rating_state: "none"`.
`close_ticket` computes the same two fields for `ticket_closed` without creating anything. The auto-resolver (`conversation_auto_resolver.py:77`) is untouched.

### 4.5 WS frames

| Frame | To | Payload |
|---|---|---|
| `rating_requested` | customer only (`to_customer=True, to_agents=False`) | `{type, request_id, token, agent_name /* first name or null */, message_id, content, source, timestamp, expires_at}` |
| `rating_request_created` | agents only | `{type, request_id, ticket_id, agent_id, source, created_at}`, **no token** |
| `ticket_resolved` / `ticket_closed` | existing audience | existing fields + `rating_request_id`, `rating_state` |

The token crosses Redis pub/sub inside `fanout_event`. That is internal; the token is never logged. There is no WS frame on rating submit (N4-D8); the dashboard reads `rating-status` on load and refocus.

### 4.6 Token redaction (staff-facing serializers)
A helper `redact_staff_meta(meta) -> meta` drops `rating_token`. It is applied in: `GET /chat/sessions/{id}`, `GET /chat/sessions/{id}/messages` (`chat.py` ~:3047, ~:3115), `visitor_service.py:517` (visitor conversation summary), and the agent WS replay. The replay sends only USER/HUMAN_AGENT frames today, so it is safe, but a test pins it. The app transcript (`agent_app.py` `TranscriptMessage`) carries no meta, so it is already safe. **Not** redacted: customer-facing `customer_conversations.py` messages and chat responses. A test walks every staff endpoint that returns messages and asserts that no `rating_token` key appears.

### 4.7 Permissions summary

| Action | support_agent | tenant_admin | customer |
|---|---|---|---|
| Request rating | only as the assignee | any ticket in tenant | – |
| See a rating's score/comment | own only (`/agent/me/ratings`, `rating-status`, scoped `/reviews/session`, `/analytics/agents/{self}`) | all in tenant | – |
| Per-agent CSAT table | – (`/analytics/agents` is admin-only today) | yes | – |
| Submit human rating | – | – | with valid token + `user_identifier` |

## 5. Customer FE (`luran-chat`)

- **`useSessionRating`**: payload gains optional `rating_request_token`. Map 400 `rating_request_invalid` / 410 `rating_request_expired` to "This rating link is no longer active." and 409 to success (as today).
- **`useWebSocket`**: handle `rating_requested` → append a `rating` message bound to `{request_id, token, agent_name}`. Handle `rating_state`/`rating_request_id` on `ticket_resolved`/`ticket_closed`.
- **`useRatingFlow`**: the rated flag is keyed **per request** (`rated_req_${request_id}`). The legacy `rated_${sessionId}` flag is kept only for the untokened (AI) prompt. On resolve/close:
  - `pending` → do not show the generic prompt; the bound card (already appended) stays.
  - `already_rated` → no prompt.
  - `none` or the field is absent (old BE) → today's generic prompt.
  - If `pending` but the token is unknown (frame missed), show **nothing**. A generic prompt here would file a human chat as an AI rating.
- **`RatingPrompt`**: optional `agentName` + `requestToken` props. Title "How was your chat with {agentName}?" (fallback "How was your chat with our team?"). Emoji scale, 500-char comment. Submit sends the token.
- **Persistence/reload:** the widget's localStorage transcript must keep the bound rating message (incl. token) so a reload before submit still works. A superseded or older card for the same agent renders **inactive** ("This request was replaced"); only the latest card per agent is live.
- **Authed views** (`ConversationDetailView` via ChatWindow, and `DashConversationView` for `/messages/[sessionId]`, which has no rating flow today): render an ASSISTANT message with `meta.system_notice === "rating_request"` as the bound card, using `meta.rating_token`/`request_id`/`agent_first_name` from server history. Hide it once rated (per-request flag), and show it inactive when it is not the latest for that agent. If the history copy and a WS frame arrive for the same request, dedupe by `request_id`/`message_id`.
- Tests (Vitest + `tsc --noEmit`): frame → card; resolve with `pending`/`already_rated`/`none`/absent; per-request flag; superseded card inactive; history-rendered card; token sent; error-copy mapping.

## 6. Dashboard (`luran-dashboard`)
- **Ticket chat** (`app/components/support/TicketActionsBar.tsx`, ticket page `support/tickets/[ticketId]`): a **Request rating** button driven by `GET /tickets/{id}/rating-status`. It is disabled with a tooltip per `reason_code` (cooldown shows the minutes left). It refreshes on `rating_request_created` and after POST. Success toast "Rating request sent". The 409 codes map to copy.
- **Ticket detail + conversation detail:** a rating badge (stars/score, source, comment on hover/expand) from `rating-status.rating` / `SessionDetailResponse.ratings`, labelled "AI" or by agent name.
- **Agents table** (`analytics/agents/page.tsx:158`): CSAT %, Avg, Count columns ("—" when null), with a note that the window applies to ratings only. **Drilldown** `analytics/agents/[agentId]`: the same three tiles.
- **Ratings page** (`analytics/ratings/page.tsx`, `components/ratings/*`, `hooks/useSessionRatings.ts`): filters Agent (select), Type (All / AI / Human), Source, date range. Columns Rated party, Ticket #. CSAT % beside the average.
- Tests: Vitest for the button states and badge, `tsc`, eslint (no new warnings), `next build`.

## 7. Agent app (`luran_agent`)
- **Takeover** (`takeover_page.dart`): a **Request rating** action (app-bar overflow or a chip near Resolve), driven by `rating-status`. A SnackBar on success / per code.
- **My ratings** (new page from Settings `_AccountRow`, `settings_page.dart:68,231`): header tiles (CSAT %, Avg, Count, "Last 30 days") + a list of the latest 20 (score, comment, date, ticket #). The empty state is "No ratings yet".
- `http_agent_api.dart`: `requestRating(ticketId)`, `getRatingStatus(ticketId)`, `getMyRatings({start, end})`. Models parse tolerantly (unknown `reason_code` → generic copy).
- Tests: API + model tests, widget tests for both screens, `flutter analyze`, iOS + Android build.

## 8. Testing (BE)
pytest on a throwaway Postgres 15 + Redis 7 (55432/56379); ruff 0.8.6 / black 24.10.0 via poetry, matching CI.
- Migration up/down on a DB with legacy ratings: `source` backfill per `meta.source`; comments encrypted and idempotent on re-run; old constraint + unique index gone; partial indexes enforce AI-once and agent-once; downgrade restores them.
- `create_request`: each refusal code; cooldown boundary; supersede on manual; reuse on auto; concurrency (two parallel creates → one request); message + meta shape; first-name fallback.
- Resolve: an assignee creates a request and the frames are ordered `rating_requested` → `ticket_resolved` with `pending`; no assignee → `none`; already rated → `already_rated`; a request failure does not fail the resolve. Close frame fields. Auto-resolver creates nothing.
- POST rating: legacy path unchanged (string errors, 409); token happy path sets `rated_agent_id`/`fulfilled_at`; invalid / other session / expired / superseded / reused token; a client-sent `rated_agent_id` is ignored; comment > 500 → 422; rate limit.
- Scoping: a support agent on `/reviews/session` sees only own; another agent's id → 403; `rating-status.rating` hidden from non-rated agents; `/agent/me/ratings` returns only own and no PII fields.
- Analytics: grouped CSAT math (≥4), null on zero, window on the rating's `created_at`.
- Redaction: no `rating_token` in any staff message endpoint; present in customer history.
- Purge: `purge_user` keeps `rated_agent_id`/`agent_id` and NULLs `requested_by`; `purge_tenant` deletes `rating_requests`. Update the module docstring's FK list and `test_account_purge_service.py`.

**Live on staging (source of the FE guide):** escalate from the widget → take over in the dashboard → manual request → card in widget → submit → rating attributed; cooldown 409; resolve → auto request reused / frames order; reload widget before submit; an authed `/messages` view renders the card from history; a support agent's scoped list; `/analytics/agents` columns; `/agent/me/ratings` from the app on a device. Capture every request/response body and WS frame.

## 9. Rollout
1. **BE → `develop` (staging)**, live-captured guide. Prod promotion can go before the FEs: everything is additive.
2. **Old clients during skew:**
   - Old widgets **ignore** `rating_requested` (the `useWebSocket` `default: break`) and the new `ticket_resolved` fields. They keep showing the generic prompt on resolve and post an untokened rating, which lands as `resolution_prompt` with no agent. That is attribution loss during the window, not breakage.
   - Old authed views render the stored "How was your chat with {name}?" as a plain assistant line with no card. This is cosmetic, so `luran-chat` should reach prod soon after the BE.
   - **Old app builds** simply lack the button and the My-ratings page. Auto-on-resolve still works for them because it is server-side.
   - Old dashboards ignore the new fields.
3. **luran-chat** → `stage` → `main`; **dashboard** → `staging` → `main`; **app** → `main`. P2/P3 run in parallel once P1 is on staging.
4. **Rollback:** FE reverts are independent. BE: revert the resolve hook (one call site) to stop auto requests. A migration downgrade is possible only while no session has more than one rating (§4.1).

## 10. Docs shipped with the work
- SOPs: `luran/docs/sop/n4_agent_rating_backend.md`; `luran-chat/docs/sop/n4_agent_rating_widget.md`; `luran-dashboard/docs/sop/n4_agent_rating_dashboard.md`; `luran_agent/docs/sop/n4_agent_rating_app.md`. Update `docs/sop/missed_ticket_lifecycle.md` only if the resolve frame change touches it (it adds fields to `ticket_resolved`).
- FE guide `luran/docs/fe-integration-guide-agent-rating.md`, live-captured, with a verification table. Traps to call out: the token appears only in the customer frame and in customer history; `rating_state` absent vs `"none"`; windowed vs all-time fields on `/analytics/agents`; string vs object `detail` on `POST /reviews/session`; `rating` null for non-rated agents.
- Master checklist (workspace `docs/checklist/luran-master-checklist.md`): an "N4 agent rating" section mapped before the build (P1 BE, P2 customer FE, P3 dashboard + app).
- Implementation packages: **P1 BE** (Sonnet implements; **Opus review of the token gate, the redaction and the migration** as security/data-critical); **P2 luran-chat**; **P3 dashboard + app**; one Opus phase review each.

## 11. Rulings most likely to be overturned (for Adebayo)
1. **Q1 — Token in message meta (N4-D4).** The alternative is no token in meta: authed customers rate through a JWT-authed endpoint, and the widget relies on localStorage only. That is smaller exposure but two submit paths. Kept as ruled, with staff redaction.
2. **Q2 — Comment encryption in the migration (N4-D7).** It changes an existing column and needs the key at migration time. The alternative is to defer encryption to a follow-up script.
3. **Q3 — No attribution backfill (N4-D1).** Existing `agent_resolved` ratings could be attributed to the ticket's assignee at resolve time. The ruling leaves them NULL because the old prompt never named the agent.
4. **Q4 — Supersede on manual re-request (N4-D3).** The alternative is to refuse a manual request while one is open (409 `rating_request_open`). Supersede lets an agent "re-ask" after 10 min, but it invalidates the older card.
5. **Q5 — 24 h post-resolve window and the scoping of `/reviews/session` for support agents (N4-D3/D5).** After resolve the widget clears its session, so a late manual request reaches mainly authed customers. Scoping the list is a behaviour change for support agents who use the ratings page today.
6. **Q6 — No tenant toggle.** Auto-on-resolve is always on. A tenant may want it off. That would be a small `Tenant.auto_request_rating` flag.
7. **Q7 — One rating per (session, agent) even across several tickets in one session.** A second ticket handled by the same agent in the same session cannot be rated again.

## 12. Risks / follow-ups
- **Insider self-rating** still exists for anyone with DB access or customer-device access. Redaction removes the casual path only.
- **Agent sees a low score from a specific ticket number** and could identify the customer. Accepted (the agent handled that ticket). There is no customer identity in the payload.
- **Skew attribution loss** until `luran-chat` ships (§9).
- **Follow-ups:** a CSAT tile on `GET /analytics/overview` (PRD 5.1 core metric) split AI vs human; customer-account deletion should scrub `session_ratings.user_identifier`/comment for that customer (not covered by today's purge either); a rating-received WS frame or push (N4-D8 v2); excluding `rating_request` notices from the AI context if they ever confuse the model.
