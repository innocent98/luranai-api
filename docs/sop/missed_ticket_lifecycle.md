# Missed-ticket lifecycle + attachment send order

**What shipped:** PR `fix/missed-ticket-lifecycle` -> `develop`. Two commits: (1) MISSED tickets are claimable and no longer stay MISSED; escalation transcript returns the newest 50 messages; data repair migration. (2) Attachments keep send order (`message_attachments.position`).

## Why
- Prod TKT-2026-0007: dashboard takeover of a MISSED ticket left `status=missed` with `assigned_to` set. `TicketService.assign_ticket` only moved OPEN -> IN_PROGRESS.
- `GET /agent/escalations/{id}` ordered ASC with `LIMIT 50`, so long chats showed the oldest 50 messages and the app transcript went stale.
- Agent composer uploads images concurrently; attachments were ordered by upload `created_at`, so [img1, img2] could reload as [img2, img1].

## How
| Change | Where |
|---|---|
| `assign_ticket`: OPEN **or MISSED** -> IN_PROGRESS (RESOLVED/CLOSED untouched) | `app/services/ticket_service.py` |
| Escalation detail: `ORDER BY created_at DESC, id DESC LIMIT 50`, reversed to ascending; response shape unchanged | `app/api/v1/endpoints/agent_app.py` |
| `/agent/escalations/{id}/acknowledge` already accepted unassigned MISSED; now covered by tests (409 for other agent, idempotent same-agent) | `agent_app.py` |
| `position` column set in `link_attachments` (index in request `attachment_ids`); relationship ordered by `(position, created_at)` so send response, history, WS and app detail all agree | `app/db/models/conversation.py`, `app/services/conversation_service.py` |

Resolve/close endpoints have no status guard; a test now pins resolving a MISSED ticket.

## Claim and resolve rules (review round)
- **Dashboard takeover** (`POST /chat/sessions/{id}/takeover`) now claims the ticket atomically (`TicketService.claim_ticket`: conditional UPDATE, not RESOLVED/CLOSED, unassigned or already mine) BEFORE touching `session.agent_id`. Loss returns 409 and changes nothing. Same-agent re-takeover is idempotent 200. Success enqueues `enqueue_clear_alert_async` (stops supervisor ring), best-effort.
- **409 body** (takeover and `/agent/escalations/{id}/acknowledge`, `AcknowledgeLost`): `{"detail": {"won": false, "handled_by": "<name>", "reason": "claimed"}}` when held by another agent; `{"detail": {"won": false, "handled_by": "another agent", "reason": "resolved"}}` when the unassigned ticket is RESOLVED/CLOSED.
- **Resolve** (`POST /tickets/{id}/resolve`): tenant admin always; an agent only if `assigned_to == self` (else 403, so unassigned MISSED needs a takeover first). Already RESOLVED/CLOSED -> 409. `/close` unchanged (admin only).
- `mt60missedrepair` also bumps `updated_at` on repaired rows.

## Round 3 (dashboard integration)
- `POST /tickets/{id}/resolve`: `resolution_notes` is optional (absent/blank -> stored null; trimmed; max 5000). Downstream (WS `ticket_resolved`, auto-resolver, DB column) already tolerate null. The dashboard enforces its own minimum client-side.
- `GET /chat/sessions/{id}` returns `ticket_id` (latest ticket of the session, nullable). `GET /tickets?session_id=<uuid>` filters (still tenant-scoped). Replaces the dashboard's last-200 scan.
- Every `agent_message` / `customer_message` frame carrying a `message_id` now includes `attachments` (id, url, content_type, width, height; send order) via `attachment_frames()` in `conversation_service.py`: replays on customer and agent sockets, live agent text frames (`[]`), `send_agent_message` and customer fanout. Root cause of dashboard "images vanish": id-merging clients overwrote the message with a frame lacking `attachments`.

- `ticket_resolved` / `ticket_closed` now also carry `rating_request_id` and `rating_state` (N4, see `n4_agent_rating_backend.md`).

## Migrations
| Revision | Purpose |
|---|---|
| `mt60missedrepair` | Data: `status::text='MISSED' AND assigned_to IS NOT NULL` -> `IN_PROGRESS`. `ticket_status` stores enum NAMES (uppercase). Uses `::text` because a fresh DB adds the MISSED value earlier in the same transaction (a bare literal raised `UnsafeNewEnumValueUsage`). Idempotent. Downgrade is a documented no-op. |
| `mp70attachpos` | Add `message_attachments.position INT NOT NULL DEFAULT 0`; backfill per message by `created_at, id`. Downgrade drops the column. |

## Verification
- Tests: `tests/agent_caller/test_missed_ticket_lifecycle.py` (11), `tests/services/test_attachment_linking.py::test_attachments_keep_send_order_not_upload_order`. Written red first (3 + 1 failing), green after the fix.
- Full suite with CI env on Postgres 15 + Redis: 1331 passed, 1 failed (`tests/db/test_deletion_request_migration.py`, which picks up a `luran_user` DSN from the local environment, unrelated to this change). ruff and black (`app/`) clean with pinned 0.8.6 / 24.10.0.
- `alembic upgrade head && downgrade -1 && upgrade head` clean on a fresh Postgres DB; repair and backfill SQL checked on seeded rows.

## Operate / roll back
Deploy runs `alembic upgrade head`. Revert code normally; `mp70` downgrades cleanly, `mt60` does not revert (repaired rows were wrongly MISSED).

## Follow-ups
- Dashboard `statusMeta` lacks `missed`; resolve button gating (`TicketDetails.tsx`) and Flutter `isClaimable` are handled in their repos.
- Customer presence is out of scope.
