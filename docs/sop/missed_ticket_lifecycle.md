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
