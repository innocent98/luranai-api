# Ticket escalation race lock + ticket-search flake

**What shipped**: (1) `_get_or_create_escalation_ticket` now takes a row lock on the conversation session and re-checks for a live ticket inside it, so concurrent escalations of one session yield one ticket. (2) Fixed a flaky ticket-search test. Branch `fix/ticket-search-flake-and-escalation-race` (no PR yet).

## 1. Duplicate-ticket race (N3 follow-up)

**Why**: the helper was check-then-create (`get_open_ticket_for_session` then `create_ticket`) with no lock. Two simultaneous escalations (two flows, a double message, router + flow) both saw no open ticket and both inserted. Reproduced with 4 threads: without the lock the second insert hit `ix_tickets_ticket_number` (count-based number generator is racy too) or created a duplicate.

**How**:
| Piece | Detail |
|---|---|
| `TicketService.lock_session_for_escalation` (`app/services/ticket_service.py`) | `SELECT id FROM conversation_sessions WHERE id=:id FOR UPDATE` |
| `_get_or_create_escalation_ticket` (`app/api/v1/endpoints/chat.py`) | lock, then existing-check, then reopen-MISSED / reuse / create. Lock released by the commit inside create/reopen, an explicit commit on the plain-reuse path, or rollback on error |

Chosen over a unique partial index on `tickets(session_id)` for live statuses: that needs a migration and would turn the race into a 500 instead of a reuse. Could be added later as defence in depth.

**Covers**: all 6 chat call sites (`_escalate_to_human` and the 5 inline ones) since they all resolve through the helper.

**Verification**: `tests/agent_caller/test_escalation_race_lock.py` (real Postgres, independent connections, committed rows, self-cleaning): (a) A holds the row lock, B must block, then reuses A's ticket; (b) 4 threads released by a barrier produce exactly 1 ticket and 1 `created=True`. Both fail on the old code.

**Not covered / follow-ups** (voice, `POST /tickets` and the ticket-number race were closed by `ticket_number_sequence_and_followups.md`)
- `voice.py::voice_escalate_to_human` (own get-then-create; `escalate_session` commits between check and create, so it needs reordering, not just a lock).
- `POST /tickets` (`tickets.py::create_ticket`) manual creation.
- `_generate_ticket_number` is count-based, so two different sessions can still collide on number.
- The 5 inline chat sites that duplicate `_escalate_to_human` logic remain un-extracted (comments `not using _escalate_to_human`).

**Rollback**: revert the commit; no schema or config change.

## 2. Flaky `test_ticket_with_visitorless_session_still_matches_own_fields`

**Root cause**: test helper `_ticket` defaulted `user_identifier` to `anon-<8 random hex>`. Search matches `user_identifier` case-insensitively, so needle `N-77` matched `anon-77xxxxxx` (1 in 256 runs), adding `TKT-N-88`. Not a product bug: the search is correct. Reproduced deterministically by forcing `anon-77deadbe`.

**Fix**: constant `anon-session` default in `tests/api/v1/test_tickets_search.py` (no uniqueness constraint on `user_identifier`, only an index).
