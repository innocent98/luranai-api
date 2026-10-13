# FE integration guide — Agent rating (N4): request a rating, rate an agent, see ratings

Backend: PR #124 (`feat/n4-agent-rating`, merged to `develop` at `53ace7d`). Spec: `docs/superpowers/specs/2026-10-13-n4-agent-rating-design.md` (decisions N4-D1..D10). SOP: `docs/sop/n4_agent_rating_backend.md`.

**Provenance.** Every JSON body below is pasted by script from `docs/fe-integration-captures/agent-rating/` (`NN-name.json`, numbered as cited), captured on 2026-10-13 against the real app run **locally** (uvicorn on 127.0.0.1, Postgres 15, Redis 7, migrations at `n4agentrating`, `AGENT_ALERT_ENABLED=false`). Staff JWTs, rating tokens and the customer identifier are replaced by labelled placeholders (`<agent-jwt-placeholder>`, `<rating-token-N-placeholder>`, `<customer-identifier-placeholder>`); the number in a token placeholder links the frame that delivered it to the request that used it. UUIDs are throwaway ids from a scratch database. Long strings are trimmed and marked `<N characters, trimmed>`. Anything not exercised live is marked **resolved-from-code** or **PENDING-HUMAN** inline and in section 11. Where a list is shortened to its first item the text says so.

## 1. What the feature is (FE terms)

A customer can rate **the human agent who helped them** (1 to 5 emoji scale, optional comment of at most 500 characters). Before this change a rating only belonged to the chat session ("the AI" or nobody); now one session can carry one rating for the AI **and** one rating per human agent.

When a rating card appears:

| Trigger | Who | What the customer gets |
|---|---|---|
| **Manual** "Request rating" button | the assigned agent, or any tenant admin, while the ticket is `in_progress` or was resolved less than 24 h ago | a `rating_requested` WS frame plus a persisted chat message "How was your chat with {first name}?" |
| **Automatic on resolve** | `POST /tickets/{id}/resolve` with an assigned agent | the same frame and message, then `ticket_resolved` |
| Close (`POST /tickets/{id}/close`) | admin | **nothing is created**; the frame only reports an already-open request |
| Auto-resolver cron | none | nothing |
| Ticket with no human agent | | nothing: the old generic prompt (AI rating) is still used |

Who reads what: customers (widget, hosted support page, authed chat) submit; **admins** see all ratings and per-agent CSAT; **support agents see only their own ratings**; viewers see none. CSAT = percentage of ratings scoring 4 or 5.

Surfaces: customer chat (`luran-chat` widget, hosted page, authed `ChatWindow` and `/messages/[sessionId]`), dashboard (ticket chat button, ratings page, analytics), Flutter agent app (takeover button, "My ratings").

## 2. State machines

**A rating request** (server side, you never see the row, only its effects):

```
open --(customer submits with its token)--> fulfilled
open --(7 days pass)----------------------> expired      (submit => 410 rating_request_expired)
open --(agent manually re-requests >10 min later)--> superseded (same 410; older card must go inactive)
```

At most one request is open per (session, agent). Reasons a manual request is refused are in section 5.

**The customer card / prompt state** is driven by the `rating_state` field on `ticket_resolved` / `ticket_closed`:

| `rating_state` | Meaning | Widget action |
|---|---|---|
| `"pending"` | an open request exists (`rating_request_id` is set) | do **not** show the generic prompt; keep (or have already appended) the bound card. If the token for that `rating_request_id` is unknown (frame missed, cleared storage) show **nothing**: a generic prompt would file a human chat as an AI rating |
| `"already_rated"` | this agent was already rated in this session | no prompt |
| `"none"` | no human assignee | today's generic prompt |
| field **absent** | old backend | today's generic prompt |

`"none"` and "absent" behave the same, but are different on the wire: treat only `"none"` as "backend knows".

## 3. Customer chat surfaces (widget, hosted page, authed chat)

1. Keep the existing customer socket (`/ws/chat/{session_id}?user_identifier=...`). On `rating_requested` append a **rating card** bound to `{request_id, token, agent_name}` and show "How was your chat with {agent_name}?" (fallback "our team" when `agent_name` is `null`).
2. **Persist the card and its token yourself** (localStorage). A socket reconnect does **not** replay it: capture 47 shows an empty frame list after a reload, and the token is only ever sent once over the socket. After a reload the token is recoverable only from history (authed users, section 6).
3. Submit with `POST /reviews/session` including `rating_request_token` (section 4.3). Never send an agent id; the server derives it from the token (verified: capture 26 sends a bogus `rated_agent_id` and it is ignored).
4. The "already rated" flag is **per request id** (`rated_req_{request_id}`), not per session. The old `rated_{sessionId}` flag only guards the generic (AI) prompt.
5. Only the **latest** card for an agent is live. If a second `rating_requested` arrives for the same agent (manual re-request), mark the older card inactive ("This request was replaced"): its token now returns 410.
6. Authed views: render any ASSISTANT message whose `meta.system_notice === "rating_request"` as the bound card, reading `meta.rating_token`, `meta.request_id`, `meta.agent_first_name` from server history. De-duplicate against the live frame by `request_id` / `message_id`.
7. Error copy: 400 `rating_request_invalid` and 410 `rating_request_expired` show "This rating link is no longer active."; 409 `already_rated` is treated as success; 429 `rating_rate_limited` shows a retry-later message.

## 4. Endpoints

All new errors use the object form `{"detail": {"code": "...", "message": "..."}}`. The older string-detail errors on `POST /reviews/session` are **unchanged** (section 5). Staff endpoints use `Authorization: Bearer <jwt>` (dashboard also accepts the cookie).

### 4.1 `POST /tickets/{ticket_id}/rating-requests` (dashboard + agent app)

Auth: tenant admin, or the support agent assigned to the ticket. No body. Success creates the request, persists the message, and fans out the two frames (section 7).

Assigned agent Sam, ticket `in_progress` (capture `10-rating-request-201.json`) -> **201**
```json
{
  "request_id": "45550168-84fe-441c-8cd4-f77a310c2b65",
  "ticket_id": "633d7c63-f992-4a99-94d3-d09d827fe8b6",
  "session_id": "2f620b02-5700-4897-870c-3ace295f42dd",
  "agent_id": "4da5516f-1d3c-4eb4-8c45-29ddf75e706e",
  "agent_name": "Sam Rivers",
  "source": "manual_request",
  "message_id": "dd27f498-b5ad-423d-9e21-ebfbec6f48bb",
  "created_at": "2026-10-13T11:30:16.066218Z",
  "expires_at": "2026-10-20T11:30:16.066218Z"
}
```
There is **no token** in this response (nor anywhere staff can read). `agent_name` here is the **full name**.

| Capture | Status | Body |
|---|---|---|
| 07 other support agent (Kim) | **403** | ``{"detail": {"code": "rating_request_forbidden", "message": "Only the assigned agent or an admin can request a rating"}}`` |
| 09 viewer | **403** | same body as 07 |
| 08 other tenant's admin | **404** | ``{"detail": "Ticket not found"}`` (string, existing form) |
| 15 repeat inside 10 minutes (assignee) | **409** | ``{"detail": {"code": "rating_request_cooldown", "message": "A rating was requested recently. Try again later", "retry_after_seconds": 598}}`` |
| 16 repeat inside 10 minutes (admin) | **409** | same body: admins are cooled down too |
| 18 no assignee | **409** | ``{"detail": {"code": "no_human_agent", "message": "No human agent is assigned to this ticket"}}`` |
| 28 customer already rated this agent | **409** | ``{"detail": {"code": "already_rated", "message": "The customer has already rated this agent for this conversation"}}`` |
| 52 ticket `closed` | **409** | ``{"detail": {"code": "ticket_not_ratable", "message": "A rating can only be requested while the ticket is in progress or within 24 hours of resolving it"}}`` |
| 53 resolved more than 24 h ago | **409** | same body as 52 |

Check order on the server: no human agent, then ticket status, then already rated, then cooldown. A manual re-request after the cooldown **supersedes** the open request and returns 201 again (capture 45):
```json
{
  "request_id": "0b661f09-3120-459a-a40a-5ff1fa298e88",
  "ticket_id": "8ad779ac-f603-45b8-b60a-c6d691dab780",
  "session_id": "8d6fb634-76d7-4142-9dbe-e7dbfd3c3271",
  "agent_id": "4da5516f-1d3c-4eb4-8c45-29ddf75e706e",
  "agent_name": "Sam Rivers",
  "source": "manual_request",
  "message_id": "18c59b52-1891-436c-a02c-001046e08681",
  "created_at": "2026-10-13T11:30:33.783086Z",
  "expires_at": "2026-10-20T11:30:33.783086Z"
}
```

### 4.2 `GET /tickets/{ticket_id}/rating-status` (dashboard + agent app)

Drives the button and the ticket badge. Same auth as 4.1 (support agents may call it for any ticket they can open). Always **200**.

Before any request (capture 06):
```json
{
  "can_request": true,
  "reason_code": null,
  "retry_after_seconds": null,
  "open_request": null,
  "rating": null
}
```
Right after a request (capture 13; admin gets the identical body, capture 14):
```json
{
  "can_request": false,
  "reason_code": "rating_request_cooldown",
  "retry_after_seconds": 598,
  "open_request": {
    "request_id": "45550168-84fe-441c-8cd4-f77a310c2b65",
    "source": "manual_request",
    "created_at": "2026-10-13T11:30:16.066218Z",
    "expires_at": "2026-10-20T11:30:16.066218Z"
  },
  "rating": null
}
```
No assignee (17):
```json
{
  "can_request": false,
  "reason_code": "no_human_agent",
  "retry_after_seconds": null,
  "open_request": null,
  "rating": null
}
```
After the customer rated, as the **rated agent** (29), `rating` is filled:
```json
{
  "can_request": false,
  "reason_code": "already_rated",
  "retry_after_seconds": null,
  "open_request": null,
  "rating": {
    "id": "59b7ee28-e9ab-4d9d-9e37-be603f648078",
    "score": 5,
    "comment": "Sam sorted it out quickly, thanks!",
    "created_at": "2026-10-13T11:30:18.231931Z",
    "rated_party": "human",
    "rated_agent_id": "4da5516f-1d3c-4eb4-8c45-29ddf75e706e",
    "rated_agent_name": "Sam Rivers",
    "source": "manual_request"
  }
}
```
Same ticket as a **different support agent** (30): `rating` is `null` and the reason is `rating_request_forbidden`, not `already_rated`:
```json
{
  "can_request": false,
  "reason_code": "rating_request_forbidden",
  "retry_after_seconds": null,
  "open_request": null,
  "rating": null
}
```
Closed ticket with a request still open (51): note `open_request` is non-null although nothing can be requested. Use `reason_code`, not `open_request`, to decide the button:
```json
{
  "can_request": false,
  "reason_code": "ticket_not_ratable",
  "retry_after_seconds": null,
  "open_request": {
    "request_id": "0e7b9b68-5713-4db7-852d-7570ae99ed36",
    "source": "manual_request",
    "created_at": "2026-10-13T11:30:39.444284Z",
    "expires_at": "2026-10-20T11:30:39.444284Z"
  },
  "rating": null
}
```
A request that expired by time (57; here also inside the 10 minute window) shows `open_request: null`:
```json
{
  "can_request": false,
  "reason_code": "rating_request_cooldown",
  "retry_after_seconds": 599,
  "open_request": null,
  "rating": null
}
```
`reason_code` values seen live: `null`, `rating_request_cooldown`, `no_human_agent`, `already_rated`, `rating_request_forbidden`, `ticket_not_ratable`.

### 4.3 `POST /reviews/session` (public, customer)

No auth. Rate limit **20 per hour per IP** (section 5). Two paths.

**Tokened path** (human rating). Request (capture 26; the body also carries a bogus `rated_agent_id`, which is ignored):
```json
{
  "session_id": "2f620b02-5700-4897-870c-3ace295f42dd",
  "user_identifier": "<customer-identifier-placeholder>",
  "score": 5,
  "comment": "Sam sorted it out quickly, thanks!",
  "rating_request_token": "<rating-token-1-placeholder>",
  "rated_agent_id": "7be9e545-e0f0-4898-9b98-77fe94cdab78"
}
```
-> **201** (the `rated_agent_id` in the answer is the server-derived one, not the one sent):
```json
{
  "score": 5,
  "comment": "Sam sorted it out quickly, thanks!",
  "meta": {},
  "id": "59b7ee28-e9ab-4d9d-9e37-be603f648078",
  "tenant_id": "8843f825-64d4-4299-b661-a87d55bec122",
  "session_id": "2f620b02-5700-4897-870c-3ace295f42dd",
  "user_identifier": "<customer-identifier-placeholder>",
  "created_at": "2026-10-13T11:30:18.231931Z",
  "updated_at": "2026-10-13T11:30:18.231931Z",
  "rated_agent_id": "4da5516f-1d3c-4eb4-8c45-29ddf75e706e",
  "source": "manual_request",
  "rating_request_id": "45550168-84fe-441c-8cd4-f77a310c2b65",
  "rated_party": "human"
}
```
Fields added to the old response: `rated_agent_id`, `source`, `rating_request_id`, `rated_party` (`"human"` or `"ai"`). `source` is `manual_request` or `auto_resolve` for tokened ratings. An automatic-on-resolve token (capture 41) behaves identically:
```json
{
  "score": 4,
  "comment": null,
  "meta": {},
  "id": "8574c2c3-a48e-4a1d-9c3b-8f6df1ffde83",
  "tenant_id": "8843f825-64d4-4299-b661-a87d55bec122",
  "session_id": "3c5f9770-b6ea-4f48-a5a1-2eefceb926a5",
  "user_identifier": "capture-visitor-c@example.test",
  "created_at": "2026-10-13T11:30:24.497369Z",
  "updated_at": "2026-10-13T11:30:24.497369Z",
  "rated_agent_id": "7be9e545-e0f0-4898-9b98-77fe94cdab78",
  "source": "auto_resolve",
  "rating_request_id": "95f5ba81-77c1-47a0-9302-61e2aaffcf11",
  "rated_party": "human"
}
```
**Legacy untokened path** (generic prompt, old widgets, tickets with no human) is unchanged and still files an AI rating (`rated_agent_id: null`, `rated_party: "ai"`). Capture 31 (request `meta.source = "agent_resolved"` maps to `source: "resolution_prompt"`):
```json
{
  "score": 3,
  "comment": "The bot part was okay",
  "meta": {
    "source": "agent_resolved"
  },
  "id": "b642f8a3-3e0d-4b5d-a56d-0577083197f5",
  "tenant_id": "8843f825-64d4-4299-b661-a87d55bec122",
  "session_id": "2f620b02-5700-4897-870c-3ace295f42dd",
  "user_identifier": "<customer-identifier-placeholder>",
  "created_at": "2026-10-13T11:30:18.280226Z",
  "updated_at": "2026-10-13T11:30:18.280226Z",
  "rated_agent_id": null,
  "source": "resolution_prompt",
  "rating_request_id": null,
  "rated_party": "ai"
}
```
It coexists with a human rating in the same session. A second untokened rating gets the old **string** 409 (32):
```json
{
  "detail": "Rating already exists for this session"
}
```
For the string-vs-object `detail` split see section 5.

### 4.4 `GET /reviews/session` (dashboard ratings page)

Query params: `rated_agent_id`, `rated_party` (`ai` or `human`), `source`, `start_date`, `end_date`, score filters and pagination as before. Each item gained `rated_agent_id`, `rated_agent_name`, `rated_party`, `source`, `rating_request_id`, `ticket_id`, `ticket_number` (the last three are `null` for untokened ratings). The response gained `csat_percent`. Capture 58 (`?rated_party=human`, **ratings trimmed to the first of 4 items**):
```json
{
  "ratings": [
    {
      "score": 5,
      "comment": "Great help",
      "meta": {},
      "id": "e5acb17d-ebc1-43ac-9689-4cee8f472b87",
      "tenant_id": "8843f825-64d4-4299-b661-a87d55bec122",
      "session_id": "8d6fb634-76d7-4142-9dbe-e7dbfd3c3271",
      "user_identifier": "capture-visitor-e@example.test",
      "created_at": "2026-10-13T11:30:37.333369Z",
      "updated_at": "2026-10-13T11:30:37.333369Z",
      "rated_agent_id": "4da5516f-1d3c-4eb4-8c45-29ddf75e706e",
      "source": "manual_request",
      "rating_request_id": "0b661f09-3120-459a-a40a-5ff1fa298e88",
      "rated_agent_name": "Sam Rivers",
      "ticket_id": "8ad779ac-f603-45b8-b60a-c6d691dab780",
      "ticket_number": "TKT-2026-0005",
      "rated_party": "human"
    }
  ],
  "total": 4,
  "average_score": 4.0,
  "csat_percent": 75.0,
  "page": 1,
  "page_size": 50
}
```
AI ratings (`?rated_party=ai`, capture 60, complete):
```json
{
  "ratings": [
    {
      "score": 3,
      "comment": "The bot part was okay",
      "meta": {
        "source": "agent_resolved"
      },
      "id": "b642f8a3-3e0d-4b5d-a56d-0577083197f5",
      "tenant_id": "8843f825-64d4-4299-b661-a87d55bec122",
      "session_id": "2f620b02-5700-4897-870c-3ace295f42dd",
      "user_identifier": "<customer-identifier-placeholder>",
      "created_at": "2026-10-13T11:30:18.280226Z",
      "updated_at": "2026-10-13T11:30:18.280226Z",
      "rated_agent_id": null,
      "source": "resolution_prompt",
      "rating_request_id": null,
      "rated_agent_name": null,
      "ticket_id": null,
      "ticket_number": null,
      "rated_party": "ai"
    }
  ],
  "total": 1,
  "average_score": 3.0,
  "csat_percent": 0.0,
  "page": 1,
  "page_size": 50
}
```
**Scoping** (behaviour change for support agents): a support agent is forced to their own ratings. Capture 61 (no filter, Sam) returns 3 of the tenant's 5 ratings, capture 62 (`rated_agent_id=<self>`) is the same, capture 64 (`rated_party=ai`) is an empty list, and a viewer gets an empty list (65), all with **200**:
```json
{
  "ratings": [],
  "total": 0,
  "average_score": null,
  "csat_percent": null,
  "page": 1,
  "page_size": 50
}
```
Asking a support agent for a colleague's id (63) -> **403**:
```json
{
  "detail": {
    "code": "rating_scope_forbidden",
    "message": "You can only view your own ratings"
  }
}
```
Admins are unscoped inside their tenant (capture 66 with `rated_agent_id=<Kim>` returns just Kim's one rating; capture 59 returns all 5).

### 4.5 `GET /analytics/agents` and `GET /analytics/agents/{agent_id}` (admin)

`/analytics/agents` is admin only (a support agent gets 403, capture 71: ``{"detail": "Not enough permissions. Tenant admin access required."}``). Each row gained `ratings_count`, `average_rating`, `csat_percent` (`null` when there are no ratings). Capture 67, **complete**:
```json
[
  {
    "agent_id": "2b67cdc7-9024-4455-a869-14c6f0707c81",
    "agent_name": "Ada Admin",
    "email": "ada.admin@example.com",
    "current_assigned_tickets": 0,
    "tickets_by_status": {},
    "total_tickets_resolved": 0,
    "ratings_count": 0,
    "average_rating": null,
    "csat_percent": null
  },
  {
    "agent_id": "4da5516f-1d3c-4eb4-8c45-29ddf75e706e",
    "agent_name": "Sam Rivers",
    "email": "sam.rivers@example.com",
    "current_assigned_tickets": 5,
    "tickets_by_status": {
      "in_progress": 2,
      "resolved": 3
    },
    "total_tickets_resolved": 3,
    "ratings_count": 3,
    "average_rating": 4.0,
    "csat_percent": 66.7
  },
  {
    "agent_id": "7be9e545-e0f0-4898-9b98-77fe94cdab78",
    "agent_name": "Kim Lee",
    "email": "kim.lee@example.com",
    "current_assigned_tickets": 2,
    "tickets_by_status": {
      "resolved": 1,
      "closed": 1
    },
    "total_tickets_resolved": 1,
    "ratings_count": 1,
    "average_rating": 4.0,
    "csat_percent": 100.0
  },
  {
    "agent_id": "a0f03dc8-dd19-47c0-9b75-ca1955e532a0",
    "agent_name": "Vic Viewer",
    "email": "vic.viewer@example.com",
    "current_assigned_tickets": 0,
    "tickets_by_status": {},
    "total_tickets_resolved": 0,
    "ratings_count": 0,
    "average_rating": null,
    "csat_percent": null
  }
]
```
Drilldown (admin or the agent themself) uses the same three fields (capture 68; the self call, 69, is identical apart from timestamps):
```json
{
  "agent_id": "4da5516f-1d3c-4eb4-8c45-29ddf75e706e",
  "agent_name": "Sam Rivers",
  "agent_email": "sam.rivers@example.com",
  "period_start": "2026-09-13T11:30:49.068764",
  "period_end": "2026-10-13T11:30:49.068764",
  "total_tickets_assigned": 5,
  "tickets_resolved": 3,
  "tickets_in_progress": 2,
  "avg_resolution_time_hours": -8.332684958518518,
  "conversations_handled": 5,
  "avg_response_time_minutes": 0.0,
  "ratings_count": 3,
  "average_rating": 4.0,
  "csat_percent": 66.7
}
```
A support agent asking for a colleague gets 403 with a **string** detail (70):
```json
{
  "detail": "You can only view your own performance metrics"
}
```
Traps on these two (section 8): the list row says `email`, the drilldown says `agent_email`; only the rating fields respect the date window; the drilldown timestamps have no timezone suffix.

### 4.6 `GET /agent/me/ratings` (agent app)

Auth: any signed-in agent (not plan-gated). Params `start_date`, `end_date` (default last 30 days). Sam (capture 72):
```json
{
  "period_start": "2026-09-13T11:30:49.109920Z",
  "period_end": "2026-10-13T11:30:49.109920Z",
  "count": 3,
  "average_score": 4.0,
  "csat_percent": 66.7,
  "recent": [
    {
      "id": "e5acb17d-ebc1-43ac-9689-4cee8f472b87",
      "score": 5,
      "comment": "Great help",
      "created_at": "2026-10-13T11:30:37.333369Z",
      "ticket_number": "TKT-2026-0005",
      "source": "manual_request"
    },
    {
      "id": "a609fabe-3e33-4030-8d81-9d6810fb699d",
      "score": 2,
      "comment": "Took long",
      "created_at": "2026-10-13T11:30:30.143645Z",
      "ticket_number": "TKT-2026-0004",
      "source": "manual_request"
    },
    {
      "id": "59b7ee28-e9ab-4d9d-9e37-be603f648078",
      "score": 5,
      "comment": "Sam sorted it out quickly, thanks!",
      "created_at": "2026-10-13T11:30:18.231931Z",
      "ticket_number": "TKT-2026-0001",
      "source": "manual_request"
    }
  ]
}
```
Kim, with an automatic rating and `comment: null` (73):
```json
{
  "period_start": "2026-09-13T11:30:49.119814Z",
  "period_end": "2026-10-13T11:30:49.119814Z",
  "count": 1,
  "average_score": 4.0,
  "csat_percent": 100.0,
  "recent": [
    {
      "id": "8574c2c3-a48e-4a1d-9c3b-8f6df1ffde83",
      "score": 4,
      "comment": null,
      "created_at": "2026-10-13T11:30:24.497369Z",
      "ticket_number": "TKT-2026-0003",
      "source": "auto_resolve"
    }
  ]
}
```
Nobody rated yet (74, an admin account):
```json
{
  "period_start": "2026-09-13T11:30:49.128567Z",
  "period_end": "2026-10-13T11:30:49.128567Z",
  "count": 0,
  "average_score": null,
  "csat_percent": null,
  "recent": []
}
```
There is no customer identity, session id or visitor name in this payload. Note the average is called `average_score` here and `average_rating` on the analytics endpoints.

### 4.7 `GET /chat/sessions/{session_id}` (dashboard conversation detail)

Gained `ratings` (every rating for the session, oldest first, AI and human). Capture 33, **only the new key shown** (the rest of the body is the usual session detail):
```json
[
  {
    "id": "59b7ee28-e9ab-4d9d-9e37-be603f648078",
    "score": 5,
    "comment": "Sam sorted it out quickly, thanks!",
    "created_at": "2026-10-13T11:30:18.231931Z",
    "rated_party": "human",
    "rated_agent_id": "4da5516f-1d3c-4eb4-8c45-29ddf75e706e",
    "rated_agent_name": "Sam Rivers",
    "source": "manual_request"
  },
  {
    "id": "b642f8a3-3e0d-4b5d-a56d-0577083197f5",
    "score": 3,
    "comment": "The bot part was okay",
    "created_at": "2026-10-13T11:30:18.280226Z",
    "rated_party": "ai",
    "rated_agent_id": null,
    "rated_agent_name": null,
    "source": "resolution_prompt"
  }
]
```
Before anyone rated, `ratings` is `[]` (capture 20).

## 5. Error reference (exact strings)

| Where | Status | `detail` |
|---|---|---|
| `POST /tickets/{id}/rating-requests` | 403 | ``{"code": "rating_request_forbidden", "message": "Only the assigned agent or an admin can request a rating"}`` |
| | 404 | ``"Ticket not found"`` |
| | 409 | ``{"code": "no_human_agent", "message": "No human agent is assigned to this ticket"}`` |
| | 409 | ``{"code": "ticket_not_ratable", "message": "A rating can only be requested while the ticket is in progress or within 24 hours of resolving it"}`` |
| | 409 | ``{"code": "already_rated", "message": "The customer has already rated this agent for this conversation"}`` |
| | 409 | ``{"code": "rating_request_cooldown", "message": "A rating was requested recently. Try again later", "retry_after_seconds": 598}`` |
| `POST /reviews/session` token not found or for another session | 400 | ``{"code": "rating_request_invalid", "message": "This rating link is not valid for this conversation"}`` |
| token already used | 409 | ``{"code": "already_rated", "message": "This conversation has already been rated for this agent"}`` |
| token superseded (46) | 410 | ``{"code": "rating_request_expired", "message": "This rating request has expired"}`` |
| token expired by time (56) | 410 | ``{"code": "rating_request_expired", "message": "This rating request has expired"}`` |
| rate limit (75) | 429 | ``{"code": "rating_rate_limited", "message": "Too many ratings from this network. Try again later", "retry_after_seconds": 3600}`` |
| comment longer than 500 (23) | 422 | pydantic list, `loc: ["body","comment"]`, `msg: "String should have at most 500 characters"` |
| wrong `user_identifier` (24) | 400 | ``"User identifier does not match session"`` |
| unknown session (25) | 400 | ``"Session not found"`` |
| second untokened rating (32) | 409 | ``"Rating already exists for this session"`` |
| `GET /reviews/session` colleague filter | 403 | ``{"code": "rating_scope_forbidden", "message": "You can only view your own ratings"}`` |
| `GET /analytics/agents/{id}` colleague | 403 | ``"You can only view your own performance metrics"`` |
| `GET /analytics/agents` as support agent | 403 | ``"Not enough permissions. Tenant admin access required."`` |

**String vs object `detail` on `POST /reviews/session`.** The three legacy errors (wrong identifier, unknown session, second untokened rating) are still **plain strings**, because old widgets match on them. Note the unknown-session and wrong-identifier cases are **400**, not 404/403. Every new error is an object. A client must branch on `typeof detail === "string"`.

The token-check order is: session exists, `user_identifier` matches, then token (invalid 400, then used 409, then expired or superseded 410). The `409` and `410` bodies do not include `retry_after_seconds`; the `429` does (3600, the window).

## 6. The token lifecycle (read this before building the card)

1. **Minted** when a request is created. 32 random bytes, URL-safe, shown once. The server stores only a hash.
2. **Delivered** to the customer only: in the `rating_requested` frame (field `token`, capture 11) and inside the persisted message's `meta.rating_token`, readable by the customer in history (capture 21, `GET /customer/conversations/{session_id}/messages`).
3. **Never given to staff.** Staff views of the same message carry `meta` **without** `rating_token`: `GET /chat/sessions/{id}/messages` (capture 19), `GET /chat/sessions/{id}` (capture 20, 33). The agent socket receives `rating_request_created` with no token (capture 12), and the 201 in 4.1 has none.
4. **Used once.** The first valid submit marks the request fulfilled; a repeat gets 409 `already_rated` (capture 27).
5. **Dies** after 7 days (410), or immediately when the same agent is manually re-requested after the cooldown (410, capture 46). A submit on a dead token is a 410, never a silent AI rating.
6. **Survives a reload** server side: the same token is accepted after the customer socket reconnects (capture 48), but the socket will not resend it (capture 47). Persist it client side.
7. **Customer history vs staff history**, same message:

Customer (21), assistant message:
```json
{
  "role": "assistant",
  "content": "How was your chat with Sam?",
  "id": "dd27f498-b5ad-423d-9e21-ebfbec6f48bb",
  "session_id": "2f620b02-5700-4897-870c-3ace295f42dd",
  "retrieved_chunk_ids": null,
  "token_count": null,
  "attachments": [],
  "meta": {
    "system_notice": "rating_request",
    "request_id": "45550168-84fe-441c-8cd4-f77a310c2b65",
    "agent_first_name": "Sam",
    "rating_token": "<rating-token-1-placeholder>"
  },
  "created_at": "2026-10-13T11:30:16.068611Z"
}
```
Staff, admin (19), same message:
```json
{
  "role": "assistant",
  "content": "How was your chat with Sam?",
  "id": "dd27f498-b5ad-423d-9e21-ebfbec6f48bb",
  "session_id": "2f620b02-5700-4897-870c-3ace295f42dd",
  "retrieved_chunk_ids": null,
  "token_count": null,
  "attachments": [],
  "meta": {
    "system_notice": "rating_request",
    "request_id": "45550168-84fe-441c-8cd4-f77a310c2b65",
    "agent_first_name": "Sam"
  },
  "created_at": "2026-10-13T11:30:16.068611Z"
}
```

## 7. WebSocket frames

All frames are JSON text. Customer socket `/ws/chat/{session_id}?user_identifier=...`; agent socket `/ws/agent/{session_id}?token=<jwt>` (Flutter) or the cookie (dashboard).

**Customer receives** `rating_requested` (capture 11; manual request):
```json
{
  "type": "rating_requested",
  "request_id": "45550168-84fe-441c-8cd4-f77a310c2b65",
  "token": "<rating-token-1-placeholder>",
  "agent_name": "Sam",
  "message_id": "dd27f498-b5ad-423d-9e21-ebfbec6f48bb",
  "content": "How was your chat with Sam?",
  "source": "manual_request",
  "timestamp": "2026-10-13T11:30:16.068611+00:00",
  "expires_at": "2026-10-20T11:30:16.066218+00:00"
}
```
`agent_name` is the **first name** here (`null` when the agent has no name). `expires_at` has a `+00:00` offset.

**Agents receive** `rating_request_created` (capture 12), no token:
```json
{
  "type": "rating_request_created",
  "request_id": "45550168-84fe-441c-8cd4-f77a310c2b65",
  "ticket_id": "633d7c63-f992-4a99-94d3-d09d827fe8b6",
  "agent_id": "4da5516f-1d3c-4eb4-8c45-29ddf75e706e",
  "source": "manual_request",
  "created_at": "2026-10-13T11:30:16.066218+00:00"
}
```

**Order on resolve with an assignee** (captures 37, 38; Kim resolves, no earlier request). Customer, in arrival order:
```json
[
  {
    "type": "rating_requested",
    "request_id": "95f5ba81-77c1-47a0-9302-61e2aaffcf11",
    "token": "<rating-token-2-placeholder>",
    "agent_name": "Kim",
    "message_id": "a99c9aab-8915-496f-a6a3-a77073d3c4d3",
    "content": "How was your chat with Kim?",
    "source": "auto_resolve",
    "timestamp": "2026-10-13T11:30:22.453400+00:00",
    "expires_at": "2026-10-20T11:30:22.450575+00:00"
  },
  {
    "type": "ticket_resolved",
    "ticket_id": "053f33f7-7cc1-4c8d-95c4-cfe347b5fd25",
    "ticket_number": "TKT-2026-0003",
    "resolution_notes": "Replacement shipped",
    "resolved_by": "Kim Lee",
    "resolved_at": "2026-10-13T11:30:22.437215+00:00",
    "rating_request_id": "95f5ba81-77c1-47a0-9302-61e2aaffcf11",
    "rating_state": "pending"
  }
]
```
Agents, in arrival order:
```json
[
  {
    "type": "rating_request_created",
    "request_id": "95f5ba81-77c1-47a0-9302-61e2aaffcf11",
    "ticket_id": "053f33f7-7cc1-4c8d-95c4-cfe347b5fd25",
    "agent_id": "7be9e545-e0f0-4898-9b98-77fe94cdab78",
    "source": "auto_resolve",
    "created_at": "2026-10-13T11:30:22.450575+00:00"
  },
  {
    "type": "ticket_resolved",
    "ticket_id": "053f33f7-7cc1-4c8d-95c4-cfe347b5fd25",
    "ticket_number": "TKT-2026-0003",
    "resolution_notes": "Replacement shipped",
    "resolved_by": "Kim Lee",
    "resolved_at": "2026-10-13T11:30:22.437215+00:00",
    "rating_request_id": "95f5ba81-77c1-47a0-9302-61e2aaffcf11",
    "rating_state": "pending"
  }
]
```
`rating_requested` (customer) and `rating_request_created` (agents) arrive **before** `ticket_resolved`, so the bound card is already in state when the widget clears its session. `ticket_resolved` itself never carries the token.

**Reuse: an open manual request, then resolve** (capture 43). The customer gets **only** `ticket_resolved` (no second `rating_requested`; the server cannot re-send a token it no longer holds). `rating_request_id` points at the earlier manual request, so the card from the manual frame stays live:
```json
[
  {
    "type": "ticket_resolved",
    "ticket_id": "6712e2a8-03cb-479a-9df2-98e329af8265",
    "ticket_number": "TKT-2026-0004",
    "resolution_notes": "Invoice corrected",
    "resolved_by": "Sam Rivers",
    "resolved_at": "2026-10-13T11:30:28.119104+00:00",
    "rating_request_id": "a7c6472b-e2fa-4230-911a-08b87f6f899e",
    "rating_state": "pending"
  }
]
```
The HTTP resolve answer carries the same `rating_request_id` (capture 42).

**Already rated** (capture 35): `ticket_resolved` with `rating_request_id: null`, `rating_state: "already_rated"`:
```json
[
  {
    "type": "ticket_resolved",
    "ticket_id": "633d7c63-f992-4a99-94d3-d09d827fe8b6",
    "ticket_number": "TKT-2026-0001",
    "resolution_notes": "Password reset sent",
    "resolved_by": "Sam Rivers",
    "resolved_at": "2026-10-13T11:30:18.332712+00:00",
    "rating_request_id": null,
    "rating_state": "already_rated"
  }
]
```
**No assignee** (capture 55): `rating_state: "none"`:
```json
[
  {
    "type": "ticket_resolved",
    "ticket_id": "f1830c30-f7b7-4cf3-95ce-4085e478117c",
    "ticket_number": "TKT-2026-0008",
    "resolution_notes": "n/a",
    "resolved_by": "Ada Admin",
    "resolved_at": "2026-10-13T11:30:44.274586+00:00",
    "rating_request_id": null,
    "rating_state": "none"
  }
]
```
**Close with an open request** (capture 50): `ticket_closed` gained the same two fields; closing never creates a request:
```json
[
  {
    "type": "ticket_closed",
    "ticket_id": "3aade2be-e716-4d27-b3cf-e96f406e905b",
    "ticket_number": "TKT-2026-0006",
    "closed_by": "Ada Admin",
    "closed_at": "2026-10-13T11:30:41.102697",
    "rating_request_id": "0e7b9b68-5713-4db7-852d-7570ae99ed36",
    "rating_state": "pending"
  }
]
```
The agent socket received an identical `ticket_closed` frame. There is no frame on submit; the dashboard refreshes `rating-status` on load, window refocus and on `rating_request_created`.

**Authed customer socket (`/ws/customer/{session_id}`)**: not exercised live; it shares the same customer connection registry, so the frame is expected to arrive (resolved-from-code, PENDING-HUMAN to confirm on a real authed session).

## 8. Field-nesting and shape traps

- **Token only for customers.** `meta.rating_token` and the frame `token` exist only on customer-facing payloads (section 6). Do not look for a token in any dashboard or app payload.
- **`rating_state` absent vs `"none"`** (section 2).
- **`agent_name` differs:** first name in the customer frame and in the persisted message (`agent_first_name`), full name in the 201 of 4.1 and in `rated_agent_name`, `resolved_by`.
- **Different timestamp formats:** `ticket_closed.closed_at` is `"2026-10-13T11:28:50.135363"` (no timezone, UTC by convention) while `ticket_resolved.resolved_at` has `+00:00`; the drilldown `period_start` / `period_end` are also naive while `/agent/me/ratings` periods end in `Z`. Parse defensively and treat naive values as UTC.
- **`rating_request_id` on the HTTP close answer is `null`** (capture 49) although the `ticket_closed` frame carries the open request id. Read the frame. On resolve, the HTTP answer and the frame agree.
- **`/analytics/agents` windowing:** only `ratings_count`, `average_rating`, `csat_percent` follow `start_date`/`end_date` (default 30 days). `total_tickets_resolved` and `current_assigned_tickets` are all-time or current. The drilldown's ticket counts are windowed.
- **Name drift:** list row `email` vs drilldown `agent_email`; `average_rating` (analytics) vs `average_score` (`/agent/me/ratings`, `/reviews/session`).
- **`rating` on `rating-status` is `null`** for support agents who are not the rated agent, and then `reason_code` is `rating_request_forbidden` even though the real reason may be `already_rated`.
- **`open_request` can be non-null while `can_request` is false** (capture 51).
- **Viewers** appear as rows in `/analytics/agents` (all tenant users are listed, existing behaviour) but get 403 on `rating-requests` and an empty `/reviews/session`.
- **RESOLVED 2026-10-13:** `GET /reviews/session` items no longer carry `user_identifier`; they carry `customer_label` (visitor name/email/phone, else `Visitor <first 8 of session_id>`). Shape from code + pytest (`tests/rating/test_staff_credential_hygiene.py`), not re-captured live. The captured list bodies above still show the old field. See `docs/sop/staff_surface_credential_hygiene.md`.
- **`comment` is `null`, not absent,** when the customer left none (captures 41, 73).
- **`rated_agent_name`** is `null` for AI ratings; `ticket_id` and `ticket_number` are `null` for untokened ratings.
- **A reused request is invisible:** after a resolve that reuses a manual request, there is no new frame or message. Do not wait for one.

## 9. UX consequences the FE must surface

- **Replaced cards become dead.** When an agent re-requests after 10 minutes, the old card's token returns 410. Render the older card inactive ("This request was replaced") and show "This rating link is no longer active." on a 410.
- **Cooldown.** Disable the dashboard / app button with the minutes left (`ceil(retry_after_seconds / 60)`, 598 s is 10 min). Admins are subject to it too.
- **Behaviour change for support agents.** The ratings page now shows only their own ratings (previously every tenant rating). Tell them; the AI tab is empty for them.
- **Auto request on resolve is always on** (no tenant toggle yet): every resolve with an assignee sends the customer a card. The dashboard should say so near the Resolve button.
- **`pending` with an unknown token shows nothing,** never the generic prompt.
- **Submitting twice is success.** `409 already_rated` should read as "thanks, already received".
- **Old clients keep working:** old widgets ignore the new frame and file the generic untokened rating (attributed to nobody); old authed views show the question as a plain assistant line.
- **Comments are customer text up to 500 characters;** render as plain text.
- **Do not show customer identity next to agent ratings in the agent app** (the payload has none; the dashboard list now carries `customer_label` instead of `user_identifier`).

## 10. Polling guidance

No polling loop. Read `GET /tickets/{id}/rating-status` on ticket open, on window refocus, after your own POST, and when a `rating_request_created` frame arrives for the open ticket. Tick the cooldown locally from `retry_after_seconds`. The ratings page and analytics are plain fetches.

## 11. Verification table

| Behaviour | Status | Evidence |
|---|---|---|
| Manual request 201 (assignee), no token in body | verified-live (local) | 10 |
| Manual request 403 (other agent, viewer), 404 (other tenant) | verified-live (local) | 07, 09, 08 |
| Manual request 409: cooldown (assignee, admin), no_human_agent, already_rated, ticket_not_ratable (closed, resolved over 24 h) | verified-live (local) | 15, 16, 18, 28, 52, 53 |
| Manual re-request after cooldown supersedes (201) | verified-live (local) | 45 (cooldown aged by editing `created_at` in the scratch DB) |
| `rating-status` all documented reasons | verified-live (local) | 06, 13, 14, 17, 29, 30, 51, 57 |
| Customer `rating_requested` frame, agent token-free frame | verified-live (local) | 11, 12 |
| Auto request on resolve, frame order | verified-live (local) | 36, 37, 38 |
| Reuse on resolve: no new frame, same request id | verified-live (local) | 42, 43, 44 |
| `already_rated` / `none` / `pending` on `ticket_resolved`; close fields | verified-live (local) | 35, 55, 37, 50 |
| Persisted message: token for customer, none for staff | verified-live (local) | 19, 20, 21 |
| Public POST tokened 201; client `rated_agent_id` ignored | verified-live (local) | 26 |
| Tokened 400 invalid, 409 reused, 410 superseded, 410 expired (time) | verified-live (local) | 22, 27, 46, 56 (expiry forced by editing `expires_at`) |
| Legacy untokened 201 and string 409; 400 strings | verified-live (local) | 31, 32, 24, 25 |
| 429 `rating_rate_limited` | verified-live (local) | 75 (from 127.0.0.1, no proxy) |
| 429 behind nginx (client IP detection) | PENDING-HUMAN | see SOP prod blockers |
| Comment over 500 characters, 422 | verified-live (local) | 23 |
| Reload before submit: token still valid, no replay | verified-live (local) | 47, 48 |
| `/reviews/session` admin, filters, `csat_percent` | verified-live (local) | 58, 59, 60, 66 |
| `/reviews/session` support agent scoped, 403 scope, viewer empty | verified-live (local) | 61, 62, 63, 64, 65 |
| `/analytics/agents`, drilldown (admin, self), colleague 403, support agent 403 | verified-live (local) | 67 to 71 |
| `/agent/me/ratings` with and without ratings | verified-live (local) | 72, 73, 74 |
| `GET /chat/sessions/{id}` `ratings` | verified-live (local) | 33, 20 |
| Authed customer history with token | verified-live (local) | 21 (customer registered through the real endpoint, email verification flag flipped in the scratch DB, signed in through the real login) |
| Authed customer socket `/ws/customer/` receives the frame | resolved-from-code | shared connection registry; PENDING-HUMAN |
| Hosted-page visitor bootstrap returns the token in its history | resolved-from-code | `visitor_service.py` is deliberately unredacted |
| Flutter app and dashboard rendering | PENDING-HUMAN | FE work (P2, P3) |
| Real widget reload, multiple tabs | PENDING-HUMAN | FE concern |
| Push or email on a new rating | not built | N4-D8 |

Harness notes: the scratch DB was the only place states were forced (cooldown age, request expiry, ticket `resolved_at`); `avg_resolution_time_hours` in capture 68/69 is negative because of that edit, not a product behaviour.
