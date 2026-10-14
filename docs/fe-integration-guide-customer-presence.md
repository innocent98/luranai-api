# FE Integration Guide - Customer Presence

**Audience:** dashboard (`luran-dashboard`), agent app (`luran_agent`, Flutter), customer surfaces (`luran-widget`, `luran-chat`).
**Backend:** branch `feat/customer-presence` (PR to `develop`). Every body and frame below was **captured live** on 2026-10-14 against the app running locally (uvicorn on a throwaway Postgres 15 + Redis 7, 2 seeded tenants, fake data only). Raw captures: `docs/captures/customer-presence/*.json` (REST bodies + `ws_frames.json`). Names, emails and IDs are throwaway seed values, not real data. Tokens and customer credentials are not in the captures.

Base path `/api/v1`. WebSocket base `/api/v1/ws`. Spec: `docs/specs/2026-10-14-customer-presence-design.md` (PR-D1..D7, workspace repo).

---

## 1. What it is

Agents see whether the customer is still there: on every list (tickets, escalations, missed, session history) and in the open conversation header.

| State | Meaning | Show as |
|---|---|---|
| `active` | customer's chat is open and the tab is visible | green "Active" |
| `away` | chat open but the tab is hidden | amber "Away" |
| `offline` | no open chat for more than ~30 s | grey "Offline", with "last seen X" when `last_seen_at` is set |

Presence **informs, never blocks**: takeover, claim and reply work in every state. No push, ring or sound on return (PR-D5).

## 2. Customer sockets (widget, hosted support page, authed customer chat)

Both customer sockets accept the same new frame:

```json
{"type": "presence", "state": "away"}
```

`state` is `"active"` or `"away"`. Anything else is ignored silently (no error frame, socket stays open).

| Socket | URL |
|---|---|
| widget + hosted support page | `/api/v1/ws/chat/{session_id}?user_identifier=...` |
| authed customer chat | `/api/v1/ws/customer/{session_id}?token=...` |

**When the customer client must send it**
1. **Nothing on connect.** An open socket counts as `active` until told otherwise. Old clients that never send the frame still produce `active` (connect) and `offline` (disconnect + ~30 s).
2. If the page is **already hidden when the socket opens**, send `away` immediately.
3. On every `visibilitychange`: hidden -> `away`, visible -> `active`.
4. **Heartbeat every ~20 s** with the current state (same frame). It is optional for liveness (the server refreshes your socket itself) but it repairs a state change the network dropped.
5. Do not send the same state more often than every 5 s: repeats are dropped silently. A *different* state sent less than 1 s after your previous accepted frame is not lost: the server applies the latest such state when the 1 s window closes (so a quick hidden -> visible ends on `active` about 1 s later). The first frame after the socket opens is always accepted. *Source: code + pytest, not live-captured.*

Customers never receive presence frames or fields (staff only).

A second tab on the same session: the existing server behaviour is unchanged (a new customer socket on a worker closes the older one with code 1000). Presence is aggregated across sockets in Redis (any `active` -> `active`; else any `away` -> `away`), but the browser tabs will not normally both stay connected. *Cross-worker aggregation verified by pytest only; the live run used one uvicorn worker.*

## 3. Agent socket frame (live)

On `/api/v1/ws/agent/{session_id}` (same auth as today, tenant-checked). Emitted **only when the aggregated state changes**, never on heartbeats. Captured verbatim:

```json
{"type": "customer_presence", "session_id": "a6fa6681-93d9-4dac-85e4-bec0d118b6cd", "state": "active", "last_seen_at": null}
{"type": "customer_presence", "session_id": "a6fa6681-93d9-4dac-85e4-bec0d118b6cd", "state": "away", "last_seen_at": null}
{"type": "customer_presence", "session_id": "a6fa6681-93d9-4dac-85e4-bec0d118b6cd", "state": "offline", "last_seen_at": "2026-10-14T00:06:05.921693+00:00"}
```

Captured timeline (`ws_frames.json`): socket open -> `active` frame; tab hidden -> `away`; **away heartbeat -> 0 frames**; tab visible -> `active`; socket closed -> a REST read 1 s later still says `active` (grace); **~30.5 s after the close** the `offline` frame arrives with `last_seen_at`; reconnecting an old client -> `active`; **close and reconnect within 3 s -> 0 frames** (no flap).

Notes the FE must handle:
- **No unsolicited frame when an agent socket connects.** The agent socket does not replay presence. Read it over REST (section 4) on every (re)connect.
- `last_seen_at` is non-null **only** on `offline` (and only if known). It is `null` for `active`/`away`.
- The frame carries `+00:00`; REST bodies carry `Z`. Both are ISO-8601 UTC; parse with a real date parser, do not compare strings.
- The frame only reaches agent sockets on **that session** (the open conversation). Lists have no presence socket: poll the REST list (suggested every 15-30 s while the list is visible, plus on window focus).
- Another tenant's agent cannot subscribe: the handshake is rejected (live: `server rejected WebSocket connection: HTTP 403`), and their REST read of the session returns 403/404 (section 5).

## 4. REST: `customer_presence` field (live)

One shape everywhere: `customer_presence: {state, last_seen_at}`, a **top-level key** on the object that represents the conversation. Bodies below are verbatim captures (trimmed only where marked).

| Endpoint | Where the key sits | Capture |
|---|---|---|
| `GET /agent/escalations` | each array item | `02_rest_active_escalations.json` |
| `GET /agent/escalations/{ticket_id}` | top level of the detail | `03_rest_away_escalation_detail.json` |
| `GET /tickets` | each item of `tickets[]` | `02_rest_active_ticket_list.json` |
| `GET /tickets/{ticket_id}` | top level | `02_rest_active_ticket_detail.json` |
| `GET /chat/tenants/{tenant_id}/sessions` | each array item (session history) | `02_rest_active_session_list.json` |
| `GET /chat/sessions/{session_id}` | top level (next to `messages`) | `02_rest_active_session_detail.json` |

Also present (same shape, **verified by pytest only, not live-captured**): ticket `PUT /tickets/{id}`, `POST .../assign`, `POST .../resolve`, `POST .../close` responses. The `POST /chat/sessions/{id}/takeover` response is wired the same way but **no test or live run exercised it** (treat as unverified). Never on customer-facing session payloads.

Escalation detail, active customer (`GET /api/v1/agent/escalations/{ticket_id}`):

```json
{
  "escalation_id": "21ab2bd5-4fdf-499f-9bec-1f5d7779c697",
  "session_id": "a6fa6681-93d9-4dac-85e4-bec0d118b6cd",
  "ticket_number": "TKT-C6C146",
  "customer_name": "Ada",
  "reason": "Refund request",
  "priority": "important",
  "status": "pending",
  "created_at": "2026-10-14T00:05:26.594044Z",
  "customer_presence": {"state": "active", "last_seen_at": null},
  "handled_by": null,
  "handled_by_user_id": null,
  "acknowledged_at": null,
  "transcript": []
}
```

Ticket detail (`GET /api/v1/tickets/{id}`), trimmed to the relevant keys:

```json
{
  "id": "21ab2bd5-4fdf-499f-9bec-1f5d7779c697",
  "session_id": "a6fa6681-93d9-4dac-85e4-bec0d118b6cd",
  "status": "open",
  "customer_presence": {"state": "active", "last_seen_at": null}
}
```

Session list item and session detail (`customer_label` sits next to it; offline, never connected):

```json
{
  "id": "096650d2-d572-4466-9e4a-f339749db26c",
  "status": "escalated",
  "customer_label": "Chidi",
  "customer_presence": {"state": "offline", "last_seen_at": null}
}
```

Offline after a visit (`05_rest_offline_session_detail.json`): `"customer_presence": {"state": "offline", "last_seen_at": "2026-10-14T00:06:05.921693Z"}`.

### Field-nesting traps
- **`customer_presence` is `null` (not absent) when presence is unavailable** (Redis outage). The key is always present on these payloads. Treat `null` as "unknown": show nothing, never "Offline". *Source: pytest; not live-captured.*
- `{"state":"offline","last_seen_at":null}` means "never seen connected" (or last-seen unknown), which is different from `null`.
- `last_seen_at` is only set when `state` is `offline`.
- On `GET /tickets` and `GET /agent/escalations` the key is **per row**, on the ticket/escalation, keyed by its `session_id`. Do not look for it on `visitor`.
- Escalation rows use `escalation_id` (the ticket id); ticket rows use `id`. Presence follows the row's `session_id`.

### Reads during the grace window
After the customer's last socket closes, REST and the live state keep reporting the **previous** state (`active` or `away`) for ~30 s (live: `04_rest_during_grace_escalation_detail.json` shows `active` 1 s after the close). This is deliberate (absorbs reloads and blips). Do not "correct" it client-side.

## 5. Tenant isolation (live)

An agent from another tenant asking for these sessions gets (`06_*`):
- `GET /agent/escalations/{ticket_id}` -> `404 {"detail":"Escalation not found"}`
- `GET /chat/sessions/{session_id}` -> `403 {"detail":"Access denied. You don't have permission to access this tenant."}`
- `GET /agent/escalations` -> their own tenant's rows only.

## 6. State machine and FE behaviour

```
            socket opens                 tab hidden
 offline ---------------> active <------------------> away
    ^                       |  \                        |
    |   ~30 s after the last|   \  all sockets closed   |
    +---- socket closed ----+----+-------- (grace) -----+
```

| Event | Frame to agent | FE action |
|---|---|---|
| any state change | `customer_presence` | update header chip and the list row for that session |
| agent socket (re)connects | none | **re-read** the session/ticket over REST and replace the chip |
| list screen visible | none (no socket) | poll REST every 15-30 s and on focus |
| worker crash | none | the REST read self-corrects within ~45 s (live, `07_worker_crash_ttl_lapse.json`: `active` 3 s after a SIGKILL, `offline` with a recovered `last_seen_at` ~55 s after). A live `offline` frame may be missed; the re-read repairs it. |

### Live-only notices (PR-D4)
"Customer left" / "Customer is back" are **ephemeral UI notices derived from the frames**: show them in the open conversation when a frame arrives (`offline` -> "Customer left", `offline` or `away` -> `active` -> "Customer is back"). The backend persists **nothing** to the transcript: they must not be added to message history, must vanish on reload, and must not trigger sound, push or badge changes.

### UX consequences to surface
- An `Offline` customer can still be replied to; the message is delivered when they return. Do not disable the composer or the claim/takeover buttons because of presence.
- `Away` does not mean gone. Use neutral wording ("Away"), not "left".
- After a reconnect the customer may briefly show `active` while the grace is still running on another tab: that is the aggregate. Do not infer from a single frame that a specific tab is open.

## 7. Verification table

| Behaviour | Verified |
|---|---|
| Customer connect -> `active` frame | live |
| `away` / back to `active` frames from `presence` frames | live |
| Same-state heartbeat emits no frame | live |
| Socket close -> ~30 s grace -> `offline` frame with `last_seen_at` | live |
| REST during grace shows previous state | live |
| Reload within grace emits no frame | live |
| Old client that never sends `presence` (connect/disconnect only) | live (the reconnect legs used plain connects) |
| REST `customer_presence` on escalations list/detail, tickets list/detail, sessions list/detail | live |
| Offline `last_seen_at` persisted to Postgres and read back | live (`05_*`; also pytest) |
| Worker killed (SIGKILL) -> offline by TTL lapse, no frame | live |
| Tenant isolation: REST 404/403, agent socket rejected | live |
| Ticket update/assign/resolve/close responses carry the field | pytest only |
| Takeover response carries the field | not verified (wired, untested) |
| Authed customer socket `/customer/{session_id}` emits presence | pytest only |
| Aggregation across API workers (several sockets, several processes) | pytest only (live run was one worker) |
| `customer_presence: null` on Redis outage | pytest only |
| Flood guard (5 s repeat drop, 1 s trailing-edge apply) behaviour | pytest only |
| Customer returns after a deploy cancelled the offline watcher: `active` frame still emitted, `last_seen_at` kept | pytest only |
| Blackholed Redis: REST degrades to `null` within ~1 s | pytest only |
| Customer clients never receive the frame | pytest only |
