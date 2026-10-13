# FE Integration Guide — Tickets Search (`q`)

**Audience:** dashboard (`luran-dashboard`) tickets list.
**Backend:** branch `feat/tickets-search` (PR to `develop`). Every body below was **captured live** on 2026-10-10 against the app running locally (uvicorn on a throwaway Postgres 15 + Redis 7, 2 seeded tenants, fake data only). Raw captures: `docs/fe-integration-captures/tickets-search/*.txt`. IDs, tokens, emails and phone numbers are **placeholders from the throwaway seed**, not real data.

Base path `/api/v1`. Auth: bearer access token (tenant user). Tenant scoping is unchanged: you only ever see your own tenant's tickets.

---

> **UPDATE 2026-10-13 (staff credential hygiene, not re-captured live):** `ticket.visitor.public_token` is **removed** from every ticket response (it was the visitor's bearer credential). The captured bodies below still show the old field; ignore it. Do not render a "Token:" line (`TicketDetails.tsx:202`).
>
> **Placeholder note:** captures are verbatim except `visitor.public_token`, whose fixture values (random, throwaway DB) are replaced by `<public-token-placeholder>`. Real values are opaque strings.

## 1. What it is
A search box on the tickets list. One new optional query param, `q`, on the existing `GET /api/v1/tickets`. Everything else about the endpoint (path, auth, response shape, ordering newest-first) is unchanged. The response is the **bare** `TicketListResponse` object (`tickets`, `total`, `page`, `page_size`) - there is no `{success, data}` envelope on this endpoint.

## 2. Semantics (what `q` matches)
- Case-insensitive **literal substring** match, OR-ed across: `ticket_number`, `summary`, and the linked visitor's `name`, `email`, `phone`. (`user_identifier` is no longer searched, 2026-10-13: it is the customer socket credential. Shape from code + pytest, not re-captured live.)
- `q` is trimmed. If shorter than **2 characters after trimming** (including empty / whitespace) it is **ignored** - you get the unfiltered list, HTTP 200, not an error.
- Max **100 characters**; longer is HTTP 422.
- `%`, `_` and `\` are literal characters (searching `50%` finds "50%", not "500"). Do not pre-escape them; just URL-encode (`%` -> `%25`).
- ANDs with `status_filter`, `assigned_to_me`, `session_id`. `limit`/`offset` page over the **filtered** set and `total` is the filtered count.
- Tickets with no visitor (anonymous session) still match on their own fields (number, summary, user identifier).

## 3. Requests and responses (all LIVE)

### 3.1 No `q` (baseline) - `GET /api/v1/tickets` -> 200
Returns all 4 of the tenant's tickets (`total: 4`); the other tenant's ticket is never included. Full body: `01_no_q.txt`.

### 3.2 `q` hits ticket number AND visitor phone - `GET /api/v1/tickets?q=0001` -> 200
```json
{
    "tickets": [
        {
            "summary": "Refund for double charge",
            "priority": "medium",
            "id": "d47df0d0-847f-4aa6-8153-955209864b19",
            "tenant_id": "0286fdc6-7a44-4174-8b3f-c08c547d2bef",
            "session_id": "b12a24b5-34dd-4125-804f-e39c152f8a6e",
            "ticket_number": "TKT-2026-0001",
            "status": "open",
            "assigned_to": null,
            "assigned_agent": null,
            "visitor": {
                "id": "ea38010d-bff0-4904-9fb9-7c4d5ae769db",
                "name": "Ada Example",
                "email": "ada@example.com",
                "phone": "+0000000001",
                "public_token": "<public-token-placeholder>",
                "first_seen_at": "2026-10-10T17:20:31.014390Z",
                "last_seen_at": "2026-10-10T17:20:31.014390Z",
                "is_blocked": false
            },
            "resolution_notes": null,
            "resolved_at": null,
            "created_at": "2026-10-10T17:20:30.999880Z",
            "updated_at": "2026-10-10T17:20:30.999880Z"
        },
        {
            "summary": "Refund still pending",
            "priority": "medium",
            "id": "03d29368-7d87-4238-a278-03b956f0d490",
            "tenant_id": "0286fdc6-7a44-4174-8b3f-c08c547d2bef",
            "session_id": "bf8d7fb8-b5ef-4cd4-b285-1405d3425613",
            "ticket_number": "TKT-2026-0004",
            "status": "resolved",
            "assigned_to": null,
            "assigned_agent": null,
            "visitor": {
                "id": "fc85d5b8-196c-411e-b490-af3386cd34c7",
                "name": "Ada Example",
                "email": "ada@example.com",
                "phone": "+0000000001",
                "public_token": "<public-token-placeholder>",
                "first_seen_at": "2026-10-10T17:20:31.024480Z",
                "last_seen_at": "2026-10-10T17:20:31.024480Z",
                "is_blocked": false
            },
            "resolution_notes": null,
            "resolved_at": null,
            "created_at": "2026-10-10T17:20:30.999880Z",
            "updated_at": "2026-10-10T17:20:30.999880Z"
        }
    ],
    "total": 2,
    "page": 1,
    "page_size": 100
}
```
**Trap:** `TKT-2026-0004` is in the result because the visitor's phone (`+0000000001`) contains `0001`, not because of its number. Search is deliberately broad; do not assume a hit is on the field the user typed.

### 3.3 Visitor email, case-insensitive - `GET /api/v1/tickets?q=ADA@EXAMPLE` -> 200
Returns `TKT-2026-0001` and `TKT-2026-0004` (both Ada's), `total: 2`. Full body: `03_q_visitor_email_case_insensitive.txt`. The same-named email on the other tenant's ticket (`TKT-2026-0900`) is absent.

### 3.4 One character is ignored - `GET /api/v1/tickets?q=a` -> 200
Identical to the no-`q` list (`total: 4`). Full body: `04_q_one_char_ignored.txt`. **FE: do not fire the request (or show "type 2+ characters") below 2 chars** - the server will not tell you it ignored the term.

### 3.5 Literal `%` - `GET /api/v1/tickets?q=50%25` -> 200
Only `TKT-2026-0002` ("Discount of 50% not applied"), `total: 1`. Full body: `05_q_literal_percent.txt`.

### 3.6 Over 100 chars - `GET /api/v1/tickets?q=<101 x's>` -> 422
```json
{
    "detail": [
        {
            "type": "string_too_long",
            "loc": [
                "query",
                "q"
            ],
            "msg": "String should have at most 100 characters",
            "input": "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
            "ctx": {
                "max_length": 100
            }
        }
    ]
}
```
**Error-shape trap:** this is FastAPI's default validation shape (`detail` array), **not** the `{success:false, error:{...}}` envelope. Cap the input at 100 chars client-side (`maxLength=100`) so it never happens.

### 3.7 With a status filter - `GET /api/v1/tickets?q=refund&status_filter=open` -> 200
Only `TKT-2026-0001` (the open one; `TKT-2026-0004` also matches "refund" but is resolved), `total: 1`. Full body: `07_q_with_status_filter.txt`.

### 3.8 Pagination over a search - `GET /api/v1/tickets?q=ada&limit=1&offset=1` -> 200
`total: 2`, `page: 2`, `page_size: 1`, one ticket (`TKT-2026-0004`). Full body: `08_q_visitor_name_pagination.txt`.

### 3.9 No match - `GET /api/v1/tickets?q=zzzz` -> 200
```json
{
    "tickets": [],
    "total": 0,
    "page": 1,
    "page_size": 100
}
```
Empty list is a normal 200, not a 404.

## 4. UX instructions for the FE
- Debounce typing (~300 ms) and send `q` only when the trimmed value is >= 2 chars; clear `q` entirely otherwise.
- Reset `offset` to 0 whenever `q` or any filter changes. Use the returned `total` for the pager (it reflects the search).
- Show an empty-state ("No tickets match 'x'") on `total == 0`; keep the term visible.
- Highlighting: the response does not say *which* field matched; highlight client-side if wanted.
- Cancel/ignore stale responses (latest keystroke wins).

## 5. Performance note
Visitor name/email/phone are encrypted at rest, so the server decrypts the (tenant-scoped, filter-narrowed) visitors-with-tickets set per search request. Fine for typical tenants; debouncing matters. See the SOP follow-ups.

## 6. Verification table

| Behaviour | Status |
|---|---|
| No `q` unchanged, tenant isolation | verified-live (3.1, 3.3) |
| Match on ticket number / visitor phone | verified-live (3.2) |
| Match on visitor email, case-insensitive | verified-live (3.3) |
| 1-char `q` ignored | verified-live (3.4) |
| `%` literal | verified-live (3.5) |
| `_` and `\` literal; summary match (user_identifier match REMOVED 2026-10-13); blank/whitespace `q` ignored | verified by pytest only (`tests/api/v1/test_tickets_search.py`), not captured live |
| >100 chars -> 422 shape | verified-live (3.6) |
| `q` + `status_filter` | verified-live (3.7) |
| Pagination `total` / `page` with `q` | verified-live (3.8) |
| No match -> empty 200 | verified-live (3.9) |
| `q` + `assigned_to_me` / `session_id` | pytest-covered for status only; not exercised live |
