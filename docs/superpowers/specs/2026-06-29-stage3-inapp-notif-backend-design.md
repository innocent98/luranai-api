# Stage 3 — AI Sales Agent · Slice 5c "In-app Notifications" — Backend Design

**Date:** 2026-06-29
**Repo:** `luran` (FastAPI · SYNC SQLAlchemy · Alembic · pytest)
**Paired FE spec:** `luran-dashboard/docs/superpowers/specs/2026-06-29-stage3-inapp-notif-dashboard-design.md`
**Builds on:** Slice 5a (`NotificationLog`, `NotifChannel.IN_APP`, `NotificationService.dispatch`, `notify_channels`). **Branch `feat/stage3-inapp-notif-backend` stacks on `feat/stage3-quote-pdf-backend` (Slice 5b PR #36).**
**Decomposition:** Slice 5 = 5a Email ✅ → 5b PDF ✅ → **5c In-app (this)** → 5d Slack → 5e CRM.
**PRD:** §3.4/§3.6 ("Notification sent to sales rep (email + in-app)"), `notify_channels.in_app`.
**Status:** Approved (brainstorm) → ready for implementation plan

---

## 1. Context & scope

Surface sales notifications in the dashboard. The 5a spine already models an `in_app` channel on `NotificationLog`; 5c (a) writes `in_app` rows on dispatch, and (b) exposes a tenant-scoped read API the dashboard bell/feed consumes. Backend half of the paired slice.

**Decisions (brainstorm):**
- **Store rendered `title`/`body` on the log at dispatch** (self-contained, stable feed) + a `read_at` for read-state.
- **In-app rows are written inline** in `dispatch` (status `sent` immediately — no ARQ job; the row *is* the notification).
- Read freshness is **polling** (dashboard side); the API is plain REST.

**In scope (BE):** `title`/`body`/`read_at` columns on `NotificationLog`; in-app dispatch (render text, write row, idempotent); a `notifications` read API (list, unread-count, mark-read, mark-all-read).

**Out of scope:** Slack (5d), CRM (5e); notification preferences/muting; real-time push; non-sales notifications.

---

## 2. Model extension

Add to `NotificationLog` (additive migration): `title: str | None`, `body: str | None` (`String`/`Text`), `read_at: datetime | None` (timestamptz). Used by the `in_app` channel; other channels leave them null. The existing dedupe unique index `(tenant_id, event, channel, entity_id, recipient)` is unchanged.

---

## 3. In-app dispatch

Extend `NotificationService.dispatch`: when `notify_channels.in_app` is truthy, for the same events that produce a rep alert (`sales_ready_lead`, `meeting_booked`, `quote_generated`):
- Render a short `(title, body)` from the event + entity via a new `_render_in_app_text(event, entity_type, entity_id) -> tuple[str, str]` (loads the lead/meeting/quote, builds e.g. `("New sales-ready lead", "John · Acme · score 90")`, `("Demo booked", "Wed Jul 7 · John · Acme")`, `("New quote", "₦62,050 · Acme")`). Missing/partial entity → a safe generic body.
- INSERT an `in_app` `NotificationLog` row with `recipient="in_app"` (fixed sentinel → one in-app notification per `(tenant, event, entity)` via the dedupe index), `status=SENT`, `title`, `body`, `read_at=NULL`. Reuse the existing `_try_log` savepoint pattern (idempotent; duplicate → skip).
- **No ARQ job** for in-app (unlike email). `dispatch` still returns only the email jobs; the in-app row is written synchronously.
- Email-channel behavior is unchanged.

---

## 4. Read API

New `app/api/v1/endpoints/notifications.py`, `router` mounted under `/notifications`, all authed via `get_current_tenant_admin` (mirror `leads.py`), scoped to `channel='in_app'` AND `tenant_id == user.tenant_id`:

- `GET /notifications?unread_only=<bool>&limit=<1..100>&offset=<int>` → `{success, data: {items: [{id, event, title, body, read_at, created_at}], total, limit, offset}}`, newest-first (`created_at DESC`).
- `GET /notifications/unread-count` → `{success, data: {count: int}}` (`read_at IS NULL`).
- `PATCH /notifications/{id}/read` → marks that tenant's notification read (`read_at = now()` if null); 404 if not found/!owned; `{success, data: {id, read_at}}`.
- `PATCH /notifications/read-all` → sets `read_at = now()` for all the tenant's unread in-app rows; `{success, data: {updated: int}}`.

A thin `NotificationReadService` (or methods on a service) handles the queries/mutations; endpoints stay thin.

---

## 5. Safety & invariants

- In-app row only when `notify_channels.in_app` truthy; idempotent (one per tenant/event/entity); `status=SENT` on write (no async).
- Read API strictly tenant-scoped (`tenant_id == user.tenant_id`) and `channel='in_app'` only — email/slack/crm rows never leak to the feed.
- `read_at` is the only mutable field; mark-read is idempotent (already-read stays its original timestamp or is left unchanged).
- No new PII beyond the short title/body derived from already-stored entities.
- 5a email path + dispatch's email-job return value unchanged.

---

## 6. Testing (pytest)

- **Dispatch in-app**: with `notify_channels.in_app` on, a dispatched event writes an `in_app` row with non-empty `title`/`body`, `status=SENT`, `read_at` null; idempotent (2nd dispatch → no new row); in_app off → no in-app row; email + in_app both on → both an email job AND an in-app row.
- **`_render_in_app_text`**: lead/meeting/quote each produce a sensible title+body; missing entity → generic body, no crash.
- **Read API**: list returns only this tenant's in_app rows newest-first; `unread_only` filters; unread-count counts `read_at IS NULL`; mark-read flips `read_at` + 404 for another tenant's id; mark-all-read updates all unread; cross-tenant isolation.
- Green `poetry run pytest`; 5a/5b suites unaffected.

---

## 7. Files

**New:** `app/api/v1/endpoints/notifications.py`, `app/services/notification_read_service.py` (or methods), `app/schemas/notification.py`, Alembic migration (3 columns), `tests/sales/test_inapp_dispatch.py`, `tests/api/v1/test_notifications_api.py`.
**Modify:** `app/db/models/notification_log.py` (3 columns), `app/services/notification_service.py` (in-app dispatch + `_render_in_app_text`), `app/api/v1/api.py` (mount the router).

---

## 8. Acceptance (PRD §3.4/§3.6)

1. When in-app notifications are enabled, each sales event (lead sales-ready / meeting booked / quote generated) produces one in-app notification with a readable title + body.
2. The dashboard can list the tenant's notifications (newest-first, unread filter), get the unread count, and mark read / mark all read.
3. Strict tenant isolation; idempotent; the email channel and 5a/5b behavior are unchanged. All new + existing tests green.
