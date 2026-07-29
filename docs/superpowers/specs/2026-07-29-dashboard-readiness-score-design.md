# Dashboard readiness-score UI — Design

**Date:** 2026-07-29
**Repos:** `luran` (backend endpoint) + `luran-dashboard` (frontend UI)
**Status:** Design approved — proceeding to implementation plan
**Grounding:** Stage 4 Website Auditor (`docs/luran_prd.md` §4). The public auditor shows a readiness
score (donut + category breakdown); the logged-in dashboard report page does not. This closes that gap.

---

## Problem

After a tenant runs an audit from the dashboard, the report page (`.../grow/auditor/[auditId]`) shows
only entry-activation stats and a "See readiness report" link — **no readiness score**. The rich score
UI (overall donut + Knowledge/Support/Conversion bars + projected-lift line) exists only in the
**public** `AuditReportView`, fed by the public report GET. The dashboard's `list_audits` returns just
`overall_score` (no category breakdown), and there is **no tenant single-audit endpoint**, so the
dashboard has no way to render the full score.

## Global Constraints

- Commits in Luran repos MUST NOT include a Claude co-author trailer.
- API additive on `/api/v1` (3 clients on v1); ownership enforced (tenant may only read its own audits).
- Follow existing patterns: `list_audits`' inline `{"success", "data"}` envelope on the BE; the
  `AuditScoreDonut`/`AuditScoreBar` primitives + react-query hooks + inline-token styling on the FE.

## Non-goals

- No change to the score computation, the entry-activation flow, or the gaps page.
- No change to the public `AuditReportView` behavior beyond extracting a shared sub-component.

---

## Design

### Backend (luran) — new tenant single-audit endpoint

`GET /api/v1/audit/{audit_id}` (tenant-admin auth), returning the full report for the caller's own
audit. Reuses the existing `_owned_report(db, audit_id, tenant_id)` guard (404 if not owned).

Response mirrors `list_audits`' inline envelope, **plus the category scores**:

```json
{"success": true, "data": {
  "id", "url", "entered_url", "status",
  "overall_score", "knowledge_score", "support_score", "conversion_score", "projected_score",
  "pages_scanned", "scan_quality", "scan_quality_reason", "created_at"
}}
```

All score fields are `int | null` (from `AuditReport`); `scan_quality` is `.value or None`. Built as an
inline dict exactly like `list_audits` (no new Pydantic model needed — matches the file's convention).

### Frontend (luran-dashboard)

1. **`AuditScoreCard` (new shared component)** — extract the existing score-card block from
   `AuditReportView.tsx` (the `card flat aud-report-score` div: `AuditScoreDonut` +
   three `AuditScoreBar`s + the projected-lift `<p>`). Props: `report` with
   `overall_score / knowledge_score / support_score / conversion_score / projected_score`
   (all `number | null`). `AuditReportView` then renders `<AuditScoreCard report={report} />` in place
   of the inline block (no visual change — same markup moved).

2. **`useAuditReport(auditId)` hook** — `GET /audit/{id}` → a typed `AuditReport` object (superset of
   `AuditListItem`: adds the four category scores). Replaces the detail page's `useAudit`, which today
   downloads the entire `/audit` list just to find one row — the new call is a single richer object.
   (`useAudit` stays exported for any other caller; grep confirms the detail page is its only user, so
   the swap is local.)

3. **Dashboard report page** (`.../grow/auditor/[auditId]/page.tsx`) — switch its `audit` source to
   `useAuditReport(auditId)` and render `<AuditScoreCard report={audit} />` at the **top of the
   completed-readable view** (the main `return` after the PageHeader, above the entry-activation stat
   grid). Only reached when the audit is `completed` + readable — the existing early returns already
   handle unreachable / render_failed / blocked / in-progress. For `thin`, the card renders above the
   existing "Partial" `AuditScanState` banner.

### Data flow

`start audit → worker scores the report (overall/knowledge/support/conversion/projected) →`
`dashboard report page: useAuditReport(id) → GET /audit/{id} → <AuditScoreCard report> (donut + bars + projected)`.

## Error handling

- Non-owned / missing audit → 404 (via `_owned_report`); the FE hook surfaces the existing error state.
- Null scores (a completed-but-thin audit, or category not computed) → donut/bars already render `—`
  / 0 gracefully (existing primitives); `projected_score` line hidden when null (existing guard).
- Unreachable/render_failed/blocked never reach the card (early returns).

## Testing

- **BE:** `GET /audit/{id}` returns 200 with all score fields for an owned audit; 404 for another
  tenant's audit / unknown id; unauthenticated → 401. Add to the existing dashboard-endpoint tests
  (`tests/auditor/test_dashboard_endpoints.py`).
- **FE:** the dashboard report page renders `AuditScoreCard` (donut value + three bar labels) for a
  completed audit via a route-mocked `GET /audit/{id}`; Playwright e2e (`tests/e2e/auditor.spec.ts`),
  matching the repo's e2e-only convention. `AuditReportView` still renders the same score block
  (regression — extraction is behavior-preserving). `npx tsc --noEmit` + `eslint .` clean.

## Operate / roll back

- Additive endpoint + additive FE component; no migration, no config. **Deploy BE first** (FE's hook
  depends on `GET /audit/{id}`); until then the dashboard page keeps working via `useAudit`.
- Roll back = revert each repo's branch independently.

## Follow-ups

- Optionally retire `useAudit` (whole-list fetch) once nothing else uses it.
