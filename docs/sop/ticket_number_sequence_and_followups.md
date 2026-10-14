# Ticket number counter + Track B platform follow-ups

**What shipped**: race-free ticket numbering, per-session lock on voice and manual ticket creation, `/tickets/unassigned` route fix, deterministic `test_crawler.py`. Branch `fix/ticket-number-sequence-and-followups` (PR to `develop`).

## 1. Ticket numbers (`TKT-YYYY-NNNN`)
**Why**: `_generate_ticket_number` was `COUNT(*)+1`. Two different sessions escalating at once read the same count and collided on `ix_tickets_ticket_number` (the session row lock from #129 only serialises one session).

**How**: new table `ticket_number_counters(year PK, last_value)`; `TicketService._generate_ticket_number` does one `INSERT ... ON CONFLICT (year) DO UPDATE SET last_value = last_value + 1 RETURNING last_value`. The row lock serialises concurrent generators until the creating transaction commits (immediately after the insert). A rollback also rolls back the increment, so no gaps. Visible format unchanged; numbers are global per year (not tenant-scoped), as before.
- Chosen over a Postgres `SEQUENCE`: the number resets every year and a sequence cannot without runtime DDL/privileges; a counter row also stays gapless on rollback and works on the SQLite test engine (dialect-specific upsert).
- Migration `t1ticketnumseq` (down_revision `n4agentrating`, file `alembic/versions/t1_ticket_number_counters.py`) creates the table and seeds each year from `MAX(numeric suffix)` of existing `TKT-YYYY-NNNN` numbers (MAX not COUNT, so deleted tickets never cause reuse; non-matching numbers ignored). Round trip verified: upgrade, downgrade -1, upgrade. **If another migration merges to `develop` first, re-point `down_revision`.**
- Files: `app/db/models/conversation.py` (`TicketNumberCounter`), `app/services/ticket_service.py`.

## 2. Voice + manual create use the #129 guard
New `TicketService.get_or_create_live_ticket(ticket_data) -> (ticket, created)`: lock session row, reuse the live ticket if any, else create; rollback on error so the lock is never left held.
- `voice.py::voice_escalate_to_human`: `escalate_session` (idempotent, commits) now runs first, then the guarded create. Previously check -> escalate (commit) -> create left a race window.
- `tickets.py::create_ticket` (`POST /tickets`): returns the existing live ticket with **200** instead of a duplicate; 201 for a new one. See `docs/fe-integration-guide-ticket-followups.md`.
- The method is now the single implementation: it also reopens a MISSED unassigned ticket and resets its ring (`reset_ring`), returning created=True. `chat.py::_get_or_create_escalation_ticket` delegates to it. Voice and manual do not enqueue the ring themselves (voice has no ring today; see its TODO), so a reopened ticket there is OPEN with fresh ring state but not yet pushed.

## 3. `GET /tickets/unassigned` 422
**Root cause**: declared after `/{ticket_id}`; FastAPI matches in order, so `unassigned` was parsed as a UUID. Moved above it (comment explains).

## 4. Flaky `tests/auditor/test_crawler.py`
**Root cause**: not async/mock misuse. `crawl_site` and `fetch_robots` gate every URL through `guard_url` -> `resolve_and_validate_host`, which does a **live** `getaddrinfo`. Tests that crawl `example.com` / `x.com` without patching it passed only while DNS answered. On a resolver blip the seed is dropped as unresolvable, the injected renderer is never called, and the test fails with "Expected mock to have been awaited". Reproduced deterministically by making `getaddrinfo` fail: 3 tests failed (`test_fetch_robots_parses_disallow`, `test_crawl_site_http_backend_uses_http_renderer_no_shared_browser`, `test_crawl_site_injected_renderer_still_overrides_backend`).
**Fix**: autouse fixture in the test module patches `network_security._resolve_ips` to a fixed public IP for hostnames (IP literals pass through so private/loopback rejection is still exercised). No production change, no retries.

## Verification
- New tests: `tests/services/test_ticket_number_sequence.py` (8-thread barrier on separate sessions; per-year reset/format; guarded create on one session), `tests/api/v1/test_tickets_followups.py` (unassigned route, manual lock + reuse, voice lock). 6 of 9 fail on the old code, all pass after.
- Full suite on throwaway PG15/Redis7: 1667 passed, 33 skipped (baseline 1658 + 9 new). ruff 0.8.6 and black 24.10.0 clean on `app/`.
- `test_crawler.py` run 30 times (alternating real DNS and DNS forced to fail): 30/30 green.
- Live capture: `docs/captures/track-b/`.

## Operate / roll back
Deploy runs the migration. Roll back: revert the commit and `alembic downgrade n4agentrating` (drops the counter table; the old generator is COUNT-based again).

## Follow-ups
- Other auditor tests may also touch live DNS; only `test_crawler.py` was in scope.
- The 5 inline chat escalation sites still duplicate `_escalate_to_human`.
