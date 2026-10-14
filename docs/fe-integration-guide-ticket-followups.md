# FE Integration Guide - Ticket follow-ups (Track B)

**Audience:** dashboard (`luran-dashboard`) tickets screens.
**Backend:** branch `fix/ticket-number-sequence-and-followups`. Bodies below were **captured live** on 2026-10-14 against the app (uvicorn on a throwaway Postgres 15 + Redis 7). Raw captures (body + headers): `docs/captures/track-b/`. IDs and the seeded numbers are placeholders from the throwaway DB.

Base path `/api/v1`. Auth: bearer access token (tenant admin for `/unassigned`; any tenant user for `POST /tickets`).

## 1. `GET /tickets/unassigned` now works (LIVE)
It returned **422** before (the path was parsed as `/{ticket_id}`). Now `200` with a **bare JSON array** of ticket objects (no envelope), same ticket shape as `GET /tickets` items. Excludes assigned and CLOSED tickets. Capture: `03-get-unassigned.body`.

```
HTTP/1.1 200 OK
[{"summary":"Capture ticket","priority":"medium","id":"72cfc850-8c27-49d0-8228-0ba3f6084df9","tenant_id":"9b7ed897-1572-446b-85c7-56cd6f0b6ec0","session_id":"707b981e-4e8a-4e5b-9d2f-52e6764b1239","ticket_number":"TKT-2026-12346","status":"open","assigned_to":null,"assigned_agent":null,"visitor":null,"resolution_notes":null,"resolved_at":null,"rating_request_id":null,"created_at":"2026-10-14T00:07:26.920839Z","updated_at":"2026-10-14T00:07:26.920839Z"}]
```
(array trimmed to one element; `visitor` is `null` here only because the seeded session has no visitor.)

A malformed id on the by-id route is unchanged: `GET /tickets/not-a-uuid` -> `422` `{"detail":[{"type":"uuid_parsing","loc":["path","ticket_id"],...}]}` (`04-get-ticket-bad-id.body`).

## 2. `POST /tickets` status code is now 201 **or 200** (LIVE)
Manual creation now reuses the session's live ticket (status OPEN, IN_PROGRESS or MISSED) instead of creating a duplicate.

| Case | Status | Body |
|---|---|---|
| No live ticket on the session | `201 Created` | the new ticket (`01-post-ticket-first.body`) |
| Session already has a live ticket | `200 OK` | **the existing ticket**, same `id` and `ticket_number` (`02-post-ticket-repeat-same-session.body`) |

**FE instruction:** treat both 200 and 201 as success and render the returned ticket; do not branch on 201. The `summary`/`priority` you sent are **not** applied to the reused ticket - show the returned values. If the UI offers "create ticket" on a session that already has one, surface "a ticket already exists for this conversation" rather than silently showing a new-looking row.

## 3. Ticket numbers
Format is unchanged (`TKT-YYYY-NNNN`, resets each year, global across tenants). Numbering continues after the highest existing number; a number never repeats. Sequence comes from the DB now, so do not assume the number equals the count of tickets that year.

## Verification
| Behaviour | Status |
|---|---|
| `GET /tickets/unassigned` 200 + shape | verified live |
| `GET /tickets/not-a-uuid` 422 | verified live |
| `POST /tickets` 201 then 200 reuse, same id/number | verified live |
| Number continues after seeded max (`...12345` -> `...12346`) | verified live |
| Voice escalation dedupe (no FE surface) | pytest only |
