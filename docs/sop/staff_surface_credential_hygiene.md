# Staff-surface credential hygiene

**What shipped:** customer bearer credentials (`visitor.public_token`, `visitor_token`/`visitor_session_token` in session meta, raw `user_identifier`) removed from every staff surface; staff get a non-secret `customer_label`. Also fixes the visitor bootstrap returning the oldest 50 messages. Branch `fix/hide-customer-credentials-from-staff` (PR pending). Closes N4 prod blocker 2 (`n4_agent_rating_backend.md`).

## Why
The customer socket authenticates with only `session_id` + `user_identifier`; `/public/visitor/bootstrap` with `public_token` returns the N4 rating token. A staff member who could read either could act as the customer and forge a rating. Session `meta` also stored the widget's `visitor_token`/`visitor_session_token` verbatim, and ticket search used ILIKE on `user_identifier` (a char-by-char recovery oracle).

## How
| Surface | Change |
|---|---|
| `GET /chat/sessions/{id}`, `GET /chat/tenants/{tid}/sessions`, `POST /chat/sessions/{id}/takeover` | new `StaffSessionResponse` / `StaffSessionDetailResponse` (no `user_identifier`, adds `customer_label`, session meta stripped of `visitor_token`, `visitor_session_token`, `rating_token`) via `ConversationService.to_staff_session` |
| Ticket responses (`/tickets*`) `visitor` | `VisitorIdentitySummary.public_token` removed |
| `GET /reviews/session` items | `user_identifier` dropped, `customer_label` added (batched, no N+1) |
| Agent WS `customer_message` frames (replay, live from both customer sockets, HTTP image fanout) | `user_identifier` replaced by `customer_label` |
| `GET /tickets?q=` | no longer matches `user_identifier` |
| Visitor bootstrap `last_conversation.messages` | newest 50 in chronological order (same pattern as agent_app `get_escalation`) |

`customer_label` = visitor name, else email, else phone, else `Visitor <first 8 chars of session_id>`. Never derived from a credential. Customer surfaces unchanged: `POST /chat/sessions`, customer history, customer WS, public rating submit still return `user_identifier`.

Rejected: keeping the key `user_identifier` with a safe value (misleading, hides the contract change); masked identifiers (still partial credential).

## Files
`app/utils/staff_redaction.py`, `app/schemas/{conversation,visitor,review,__init__}.py`, `app/services/{conversation_service,ticket_service,rating_read_service,visitor_service}.py`, `app/api/v1/endpoints/{chat,websocket,tickets}.py`. No migration.

## Verification
`tests/rating/test_staff_credential_hygiene.py` (16 tests: each staff endpoint and frame asserts no secret/`public_token`/`user_identifier`; customer endpoints keep it; bootstrap newest-50). Written red first (12 failed). Full suite 1656 passed, 33 skipped; ruff and black clean. Not exercised live against a running stack.

## Roll back
Revert the commit; no data changes.

## Follow-ups
- FE must switch to `customer_label` (dashboard `ConversationList.tsx`, `ConversationDetails.tsx`, `conversations/page.tsx`, `dashboard/page.tsx`, `CreateTicketModal.tsx`, ratings tables and `ratings-csv.ts`, `useConversations.ts`/`useTickets.ts` types; agent app `lib/data/ws/agent_socket.dart:207`).
- `GET /tickets/unassigned` is shadowed by `/{ticket_id}` (returns 422); pre-existing, unrelated.
- The public bootstrap still hands the rating token to anyone holding `visitor_token`; fine now that staff cannot obtain it.
- Intermittent `tests/auditor/test_crawler.py` failures (mock not awaited) are pre-existing flakes.
