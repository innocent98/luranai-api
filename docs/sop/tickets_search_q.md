# SOP - Tickets list search (`q`)

**Status:** Shipped to PR (`feat/tickets-search` -> `develop`) - 2026-10-10
**Area:** `app/api/v1/endpoints/tickets.py`, `app/services/ticket_service.py`, `tests/api/v1/test_tickets_search.py`

## What shipped
`GET /api/v1/tickets` accepts optional `q`: a case-insensitive literal substring search over ticket number, summary, session `user_identifier` and the linked visitor's name/email/phone. No migration. FE contract: `docs/fe-integration-guide-tickets-search.md`.

## Why
Feature N1: dashboard agents need to find a ticket by number or by who raised it instead of scrolling a paginated list.

## How
- Endpoint: `q: str | None = Query(None, max_length=100)` (422 beyond 100), passed to `TicketService.get_tenant_tickets(q=...)`.
- Service: filters are collected into a list shared by the main query, the count and the visitor candidate lookup. `q` is stripped; `< 2` chars -> ignored. `%`, `_`, `\` are escaped and used with `ESCAPE '\'`.
- Ticket fields use `ILIKE`. The session `user_identifier` match is an `IN (subquery)` on `Ticket.session_id` (tenant-scoped). **No joins are added to the main statement**, so rows cannot duplicate and count/page stay correct; tickets with no session-visitor still match on their own fields.
- **Key deviation from the approved design:** `Visitor.name/email/phone` are `EncryptedString` (Fernet, non-deterministic), so SQL `ILIKE` cannot match them. They are matched in Python after decryption: select distinct `(id, name, email, phone)` for visitors of this tenant who have a ticket passing the other filters, `casefold()`-substring them, then add `Ticket.session_id IN (sessions of matching visitor ids)` to the OR. Semantics are identical to ILIKE for the caller.
- Rejected: matching only `email_hash`/`phone_hash` (exact-only, no substring, no name); adding a plaintext/trigram index on PII (undoes encryption-at-rest).
- Tenant scoping: `Ticket.tenant_id`, `Visitor.tenant_id` and `ConversationSession.tenant_id` are all constrained.

## Verification
- `tests/api/v1/test_tickets_search.py` (20 tests, real Postgres): each field, case-insensitivity, strip, <2 ignored, blank ignored, 422 at 101, literal `%` `_` `\`, status combination, pagination/total with multi-field matches (no duplicates), tenant isolation, visitor-less session, NULL visitor fields.
- Full suite: 1421 passed, 33 skipped (Postgres 15 + Redis 7). `ruff check app/` and `black --check app/` clean on pinned 0.8.6 / 24.10.0.
- Live captures: `docs/fe-integration-captures/tickets-search/`.

## Operate / roll back
No config, no migration. Revert the commit; `q` becomes an unknown (ignored) query param.

## Follow-ups
- Visitor matching cost is O(visitors-with-tickets for the tenant, after other filters) decrypts per `q` request. Fine now; if a tenant gets very large, options are a search-token/blind-index table or capping candidates. No trigram index was added (ticket number/summary ILIKE is a seq scan within the tenant's rows - add `pg_trgm` GIN indexes if it shows up in slow-query logs).
- 422 for `q` > 100 uses FastAPI's default `detail` shape, not the project error envelope (pre-existing app-wide behaviour for query validation).
