# Visit tracking: idempotent upsert (fix duplicate-key 500)

## What shipped
`VisitAnalyticsService.record_visit` now records a visit with an atomic
`INSERT ... ON CONFLICT DO UPDATE` upsert instead of a check-then-insert.
Commit `2bac546` on branch `fix/visit-analytics-idempotent`.

## Why
`POST /api/v1/analytics/visit` intermittently returned **HTTP 500**:

```
psycopg2.errors.UniqueViolation: duplicate key value violates unique constraint
"uq_daily_visits_per_visitor_day"
DETAIL: Key (visit_date, tenant_id, source, visitor_key)=(...) already exists.
```

**Root cause — check-then-insert race.** The old code did `SELECT` the daily
bucket, then `INSERT` a new row when absent. Two near-simultaneous first-visits
for the same `(visit_date, tenant_id, source, visitor_key)` both saw "no row" and
both `INSERT`ed; the second `commit()` violated `uq_daily_visits_per_visitor_day`.
The `except` clause caught only `ProgrammingError`/`OperationalError` (missing
table), not `IntegrityError`, so the violation surfaced as a 500. Observed with
two dashboard visits from the same authenticated user landing together.

## How
`app/services/visit_analytics_service.py` — `record_visit`:
- `pg_insert(DailyVisit).values(...).on_conflict_do_update(constraint="uq_daily_visits_per_visitor_day", set_=...)`.
- On conflict: `visit_count = daily_visits.visit_count + 1`, `last_seen_at = now`.
- `meta` is a `JSON` column (not `JSONB`), so there is no SQL-side merge: `meta` is
  overwritten with the latest event's value **only when the new visit carries
  meta** (a meta-less repeat keeps the stored value). This is a minor change from
  the old Python-side dict-merge; acceptable for the small `{page_path: ...}` payload.
- `id` is passed explicitly (`uuid4()`) because the core `pg_insert` path does not
  apply the model's Python-side default.

### Scope / callers
Both call sites pass a **non-null** `tenant_id`, which is what the unique
constraint keys on, so the upsert deduplicates correctly:
- `app/api/v1/endpoints/analytics.py:71` — dashboard (`current_user.tenant_id`).
- `app/api/v1/endpoints/widget.py:420` — chat widget (`tenant.id`).

## Verification
`tests/test_visit_analytics_upsert.py` (Postgres-backed; uses the `pg_engine` /
`db_session` fixtures):
- `test_record_visit_race_does_not_500` — **deterministic** race reproduction: a
  holder session holds the unique-key lock uncommitted while a racer's
  `record_visit` blocks on it, then the holder commits. Verified it **FAILS on the
  old code** with the exact production `UniqueViolation` and **passes on the fix**
  (single row, `visit_count == 2`).
- `test_record_visit_repeat_increments_single_row` — idempotent increment + meta
  behavior (latest-meta kept; meta-less repeat does not clobber).

`ruff` + `black` clean. Tests run green against local Postgres 15.18.

## Operate / roll back
Pure application-code change — **no migration, no schema change**. Roll back by
reverting commit `2bac546`.

## Follow-ups
- **Null-tenant caveat (not reachable today):** Postgres treats `NULL` as distinct
  in unique constraints, so if a future caller passes `tenant_id=None`, the upsert's
  `ON CONFLICT` would not match and anonymous visits would create duplicate rows
  rather than incrementing. All current callers pass a non-null tenant. If a
  null-tenant path is ever added, migrate the constraint to
  `UNIQUE NULLS NOT DISTINCT` (available on PG15) so the upsert stays correct.
