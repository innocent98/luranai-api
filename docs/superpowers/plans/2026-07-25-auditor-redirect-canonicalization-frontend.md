# Auditor Redirect Canonicalization — Frontend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface the backend redirect-confirmation contract in the UI — on a `409 REDIRECT_CONFIRMATION_REQUIRED`, show a confirm modal and re-submit with `confirmed_target`; and show "Scanned <canonical> (you entered <entered>) · N pages reached" on both auditor report views.

**Architecture:** One small backend addition (expose `url` on the public report and `entered_url` on the dashboard audit list). In `luran-dashboard`: a shared 409-detection helper, a reusable confirm modal, threaded `confirmed_target` through the two submit hooks, wiring into all four submit surfaces, and a transparency line on both report views.

**Tech Stack:** Backend — Python/FastAPI (repo `luran`). Frontend — Next.js (App Router) + React + react-query + axios, inline-style design tokens, Playwright e2e (repo `luran-dashboard`). No FE unit-test harness exists — see Verification model.

## Repos & branches

- **Task 1 only** is in the backend repo `/Users/adebayovictor/Documents/dev/luran/luran` — put it on a feature branch off `develop`.
- **Tasks 2–8** are in `/Users/adebayovictor/Documents/dev/luran/luran-dashboard` — put them on a feature branch off `staging` (the dashboard's integration branch).

## Global Constraints

- The 409 body shape is fixed by the backend: `{ "success": false, "error": { "code": "REDIRECT_CONFIRMATION_REQUIRED", "message": ..., "details": { "entered_url": ..., "redirect_target": ... } } }`. The FE reads `err.response.data.error.code` and `.details`.
- Re-submit must send the **same** original URL plus `confirmed_target = redirect_target` (the value the modal showed). Do not mutate the entered URL.
- Tier-1 (`www`/scheme) redirects never reach the FE — the backend follows them silently; the modal is only for `different_host`.
- Match the existing modal conventions (`AuditEditModal.tsx`): inline styles with design tokens, scrim `zIndex:999` / dialog `zIndex:1000`, `role="dialog" aria-modal="true"`, Escape-to-close, `Btn` primitive. No new modal library.
- Commits must NOT include a Claude co-author trailer.

## Verification model (no FE unit harness)

- **Per-task FE check:** `npx tsc --noEmit` (types) and `npm run lint` (eslint). Run `npm install` first if `node_modules` is absent.
- **Behavior:** Playwright e2e, route-mocked, in `tests/e2e/auditor.spec.ts` — added as the final task (Task 8). Running it needs the Next dev server; command: `npm run build && npm run test:e2e` (or `PLAYWRIGHT_BASE_URL=... npm run test:e2e` against a running `npm run dev`). Per-task work is proven by tsc+lint; Task 8 proves behavior.
- **Task 1 (backend)** uses the pytest-in-Docker loop from the backend plan (`docs/superpowers/plans/2026-07-25-auditor-redirect-canonicalization-backend.md`, "Running the test suite"), DB `luran_citest` on host :5544.

## Parallelization map (dispatch waves)

Tasks within a wave touch **disjoint files** and may be implemented concurrently. Do not start a wave until the previous wave's tasks are reviewed and merged into the branch.

- **Wave 1 (parallel): T1, T2, T3** — backend repo / FE types+hooks / FE new files. No shared files.
- **Wave 2 (parallel): T4, T5, T6, T7** — each touches one distinct file. All depend on **T2** (types+hooks); T4/T5/T6 also depend on **T3** (modal+helper); T7 also depends on **T1** (real `url` data) but compiles against T2.
- **Wave 3: T8** — the e2e spec; depends on T4–T7.

```
Wave 1:  T1(BE)   T2(types+hooks)   T3(modal+helper)
              \        |        /
Wave 2:   T4(public page) T5(dash list page) T6(dash report page) T7(public report view)
                                   \        |        /
Wave 3:                          T8(playwright e2e)
```

Per-task dependency notes are repeated in each task's **Depends on / Parallel with** line.

---

## File Structure

- `luran/app/schemas/auditor.py` — add `url` to `PublicReportResponse` (T1).
- `luran/app/api/v1/endpoints/public_auditor.py` — populate `url` in the report GET (T1).
- `luran/app/api/v1/endpoints/auditor.py` — add `entered_url` to `list_audits` rows (T1).
- `luran-dashboard/app/types/auditor.ts` — new fields + error-shape (T2).
- `luran-dashboard/app/hooks/useAuditor.ts` — thread `confirmed_target` (T2).
- `luran-dashboard/app/lib/redirect-confirm.ts` — new 409 helper (T3).
- `luran-dashboard/app/components/luran/auditor/RedirectConfirmModal.tsx` — new modal (T3).
- `luran-dashboard/app/auditor/page.tsx` — public wiring (T4).
- `luran-dashboard/app/(dashboard)/dashboard/grow/auditor/page.tsx` — dashboard list-page wiring (T5).
- `luran-dashboard/app/(dashboard)/dashboard/grow/auditor/[auditId]/page.tsx` — dashboard report wiring + transparency (T6).
- `luran-dashboard/app/components/luran/auditor/AuditReportView.tsx` — public transparency (T7).
- `luran-dashboard/tests/e2e/auditor.spec.ts` — e2e (T8).

---

## Task 1: Backend — expose `url` (public report) and `entered_url` (dashboard list)

**Repo:** `luran` (feature branch off `develop`).
**Depends on:** nothing. **Parallel with:** T2, T3.

**Files:**
- Modify: `app/schemas/auditor.py:27-38` (`PublicReportResponse`)
- Modify: `app/api/v1/endpoints/public_auditor.py` (report GET handler that builds `PublicReportResponse`)
- Modify: `app/api/v1/endpoints/auditor.py:143-155` (`list_audits` row dict)

**Interfaces produced:**
- `PublicReportResponse.url: str` (the canonical scanned URL). `entered_url`/`pages_scanned` already present.
- `list_audits` rows gain `"entered_url": r.entered_url` (already emit `url`, `pages_scanned`).

- [ ] **Step 1: Add `url` to the public report schema**

In `app/schemas/auditor.py`, add to `PublicReportResponse` (after `status`):

```python
class PublicReportResponse(AuditScoresResponse):
    id: UUID
    status: str
    url: str
    pages_scanned: int
    entered_url: str | None = None
    gaps: list[BlurredGap]
```

- [ ] **Step 2: Populate `url` in the report GET handler**

In `app/api/v1/endpoints/public_auditor.py`, find where `PublicReportResponse(...)` is constructed in the report GET handler and add `url=report.url,` alongside the existing `entered_url=report.entered_url` / `pages_scanned=...` assignments. (Grep the handler for `PublicReportResponse(` to locate it.)

- [ ] **Step 3: Add `entered_url` to the dashboard list rows**

In `app/api/v1/endpoints/auditor.py` `list_audits`, add `"entered_url": r.entered_url,` to the per-row dict (next to `"url": r.url,` and `"pages_scanned": r.pages_scanned,`).

- [ ] **Step 4: Extend the public report test to assert `url`**

Locate the existing public report GET test (grep `tests/auditor` for `PublicReportResponse` or the report GET route). Add an assertion that the serialized response includes `url` equal to the report's canonical url. If a dashboard `list_audits` test exists, assert a row carries `entered_url`. Run:

```
docker run --rm -v "$PWD":/app -w /app -e DATABASE_URL="postgresql://test_user:test_password@host.docker.internal:5544/test_db" luran-api:latest sh -c "pip install -q pytest==8.3.4 pytest-asyncio==0.24.0 pytest-mock==3.14.0 pytest-cov==6.0.0 >/dev/null 2>&1 && python -m pytest tests/auditor/ -p no:cacheprovider -o addopts='' -q --ignore=tests/auditor/test_chat_grounding.py --ignore=tests/auditor/test_service_import.py"
```
Expected: all pass. Then `python -m ruff check app/` → clean.

- [ ] **Step 5: Commit**

```bash
git add app/schemas/auditor.py app/api/v1/endpoints/public_auditor.py app/api/v1/endpoints/auditor.py tests/auditor/
git commit -m "feat(auditor): expose url on public report and entered_url on dashboard list"
```

---

## Task 2: FE types + thread `confirmed_target` through the submit hooks

**Repo:** `luran-dashboard` (feature branch off `staging`).
**Depends on:** nothing (compiles standalone). **Parallel with:** T1, T3.

**Files:**
- Modify: `app/types/auditor.ts:19-48`
- Modify: `app/hooks/useAuditor.ts:16-24, 50-60`

**Interfaces produced:**
- `PublicAuditReport` gains `url: string` and `entered_url: string | null`.
- `AuditListItem` gains `entered_url?: string | null` and `pages_scanned?: number`.
- `useStartPublicAudit` and `useStartAudit` accept mutation variables `{ url: string; confirmed_target?: string }`.

- [ ] **Step 1: Add fields to the report/list types**

In `app/types/auditor.ts`, add to `PublicAuditReport`:

```tsx
export interface PublicAuditReport {
  id: string;
  status: AuditStatus;
  url: string;
  entered_url: string | null;
  pages_scanned: number;
  // ...existing fields unchanged...
}
```

and to `AuditListItem`:

```tsx
export interface AuditListItem {
  id: string;
  url: string;
  entered_url?: string | null;
  pages_scanned?: number;
  status: AuditStatus;
  // ...existing fields unchanged...
}
```

- [ ] **Step 2: Thread `confirmed_target` into the public mutation**

In `app/hooks/useAuditor.ts`, change `useStartPublicAudit` variables and body:

```tsx
export function useStartPublicAudit() {
  return useMutation<StartPublicAuditResponse, unknown, { url: string; confirmed_target?: string }>({
    mutationFn: async (body) => {
      const { data } = await apiClient.post<ApiEnvelope<StartPublicAuditResponse>>(
        "/public/audit",
        body,
      );
      return unwrap(data);
    },
  });
}
```

- [ ] **Step 3: Thread `confirmed_target` into the dashboard mutation**

In the same file, change `useStartAudit`:

```tsx
export function useStartAudit() {
  return useMutation<{ audit_report_id: string }, unknown, { url: string; confirmed_target?: string }>({
    mutationFn: async (body) => {
      const { data } = await apiClient.post<ApiEnvelope<{ audit_report_id: string }>>(
        "/audit",
        body,
      );
      return unwrap(data);
    },
  });
}
```

(Keep the exact response type/envelope the current code uses — only the variables type and the forwarded `body` change.)

- [ ] **Step 4: Typecheck + lint**

```bash
npm install   # if node_modules absent
npx tsc --noEmit
npm run lint
```
Expected: no errors. (Existing call sites `startPublicAudit.mutate({ url })` / `startAudit({ url })` remain valid because `confirmed_target` is optional.)

- [ ] **Step 5: Commit**

```bash
git add app/types/auditor.ts app/hooks/useAuditor.ts
git commit -m "feat(auditor): add url/entered_url types; thread confirmed_target through submit hooks"
```

---

## Task 3: FE — 409 helper + reusable RedirectConfirmModal

**Repo:** `luran-dashboard`.
**Depends on:** nothing (self-contained). **Parallel with:** T1, T2.

**Files:**
- Create: `app/lib/redirect-confirm.ts`
- Create: `app/components/luran/auditor/RedirectConfirmModal.tsx`

**Interfaces produced:**
- `getRedirectConfirmation(err: unknown): { entered: string; target: string } | null`
- `<RedirectConfirmModal entered target busy? onConfirm onCancel />`

- [ ] **Step 1: Write the 409 helper**

Create `app/lib/redirect-confirm.ts`:

```ts
export interface RedirectConfirmation {
  entered: string;
  target: string;
}

/**
 * If an API error is the backend's 409 REDIRECT_CONFIRMATION_REQUIRED, return
 * the entered URL and the redirect target; otherwise null. Reads the axios
 * error body { error: { code, details: { entered_url, redirect_target } } }.
 */
export function getRedirectConfirmation(err: unknown): RedirectConfirmation | null {
  if (typeof err !== "object" || err === null || !("response" in err)) return null;
  const resp = (err as { response?: { status?: number; data?: unknown } }).response;
  if (!resp || resp.status !== 409) return null;
  const data = resp.data as
    | { error?: { code?: string; details?: { entered_url?: string; redirect_target?: string } } }
    | undefined;
  const error = data?.error;
  if (!error || error.code !== "REDIRECT_CONFIRMATION_REQUIRED") return null;
  const entered = error.details?.entered_url;
  const target = error.details?.redirect_target;
  if (!entered || !target) return null;
  return { entered, target };
}
```

- [ ] **Step 2: Verify the `Btn` primitive's props**

Grep `app/components/luran/dashboard/primitives.tsx` for the `Btn` definition; confirm it accepts `kind: "primary" | "secondary"` and a `disabled` prop. If `disabled` is not supported, use the button without it and gate clicks via the `busy` guard in the handlers instead (note which you did).

- [ ] **Step 3: Write the modal**

Create `app/components/luran/auditor/RedirectConfirmModal.tsx`:

```tsx
"use client";

import { useEffect } from "react";

import { Btn } from "@/app/components/luran/dashboard/primitives";

export interface RedirectConfirmModalProps {
  entered: string;
  target: string;
  busy?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

export function RedirectConfirmModal({
  entered,
  target,
  busy,
  onConfirm,
  onCancel,
}: RedirectConfirmModalProps) {
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape" && !busy) onCancel();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onCancel, busy]);

  return (
    <>
      <div
        aria-hidden="true"
        onClick={() => {
          if (!busy) onCancel();
        }}
        style={{
          position: "fixed",
          inset: 0,
          background: "color-mix(in srgb, var(--ink) 48%, transparent)",
          zIndex: 999,
        }}
      />
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Confirm redirect"
        style={{
          position: "fixed",
          top: "50%",
          left: "50%",
          transform: "translate(-50%,-50%)",
          zIndex: 1000,
          width: "min(480px, calc(100vw - 32px))",
          background: "var(--surface)",
          border: "1px solid var(--border)",
          borderRadius: "var(--r-lg)",
          boxShadow: "var(--shadow-lg)",
          padding: 24,
          display: "flex",
          flexDirection: "column",
          gap: 16,
        }}
      >
        <h2 style={{ fontSize: 18, fontWeight: 600, color: "var(--ink)" }}>This site redirects</h2>
        <p style={{ fontSize: 14, lineHeight: 1.6, color: "var(--muted)" }}>
          You entered <strong style={{ color: "var(--ink)" }}>{entered}</strong>, which redirects to{" "}
          <strong style={{ color: "var(--ink)" }}>{target}</strong>. Scan that instead?
        </p>
        <div style={{ display: "flex", justifyContent: "flex-end", gap: 10, marginTop: 4 }}>
          <Btn kind="secondary" onClick={onCancel} disabled={busy}>
            Cancel
          </Btn>
          <Btn kind="primary" onClick={onConfirm} disabled={busy}>
            {busy ? "Starting…" : "Yes, scan it"}
          </Btn>
        </div>
      </div>
    </>
  );
}
```

- [ ] **Step 4: Typecheck + lint**

```bash
npx tsc --noEmit
npm run lint
```
Expected: no errors.

- [ ] **Step 5: Commit**

```bash
git add app/lib/redirect-confirm.ts app/components/luran/auditor/RedirectConfirmModal.tsx
git commit -m "feat(auditor): redirect-confirmation helper + reusable confirm modal"
```

---

## Task 4: Public auditor wiring

**Repo:** `luran-dashboard`.
**Depends on:** T2 (hook accepts `confirmed_target`), T3 (helper + modal). **Parallel with:** T5, T6, T7.

**Files:**
- Modify: `app/auditor/page.tsx` (`handleSubmit` ~564-595, the `AuditScanState onRetry` ~638-669, render tree ~620-678)

**Interfaces consumed:** `getRedirectConfirmation`, `RedirectConfirmModal`, `useStartPublicAudit` (with `confirmed_target`).

- [ ] **Step 1: Import the helper + modal**

Add at the top of `app/auditor/page.tsx`:

```tsx
import { getRedirectConfirmation } from "@/app/lib/redirect-confirm";
import { RedirectConfirmModal } from "@/app/components/luran/auditor/RedirectConfirmModal";
```

- [ ] **Step 2: Add prompt state and a single shared submit function**

Add state alongside the existing `useState` calls:

```tsx
const [redirectPrompt, setRedirectPrompt] = useState<{ entered: string; target: string } | null>(null);
```

Add a shared `runAudit` (used by both the initial submit and the retry), replacing the duplicated `startPublicAudit.mutate(...)` bodies:

```tsx
const runAudit = useCallback(
  (confirmedTarget?: string) => {
    setInlineError(null);
    startPublicAudit.mutate(
      { url: url.trim(), ...(confirmedTarget ? { confirmed_target: confirmedTarget } : {}) },
      {
        onSuccess: (data) => {
          setRedirectPrompt(null);
          const claim: ClaimCookie = {
            auditId: data.audit_report_id,
            claimToken: data.claim_token,
          };
          persistClaim(claim);
          setAuditId(data.audit_report_id);
          setClaimToken(data.claim_token);
          setPhase("scanning");
        },
        onError: (err) => {
          const confirmation = getRedirectConfirmation(err);
          if (confirmation) {
            setRedirectPrompt(confirmation);
            return;
          }
          setRedirectPrompt(null);
          if (isAxiosError(err) && err.response?.status === 429) {
            setPhase("limit");
          } else {
            setInlineError("We couldn't start the scan. Check the URL and try again.");
          }
        },
      },
    );
  },
  [url, startPublicAudit],
);
```

Make `handleSubmit` (and the `AuditScanState` `onRetry` handler) call `runAudit()`:

```tsx
function handleSubmit() {
  if (!url.trim()) {
    setInlineError("Enter a website URL to scan.");
    return;
  }
  runAudit();
}
```

Ensure `useCallback` is imported from React (add to the existing `import { useState, ... } from "react"`).

- [ ] **Step 3: Render the modal**

Near the end of the returned JSX (top level of the fragment, so it overlays every phase), add:

```tsx
{redirectPrompt && (
  <RedirectConfirmModal
    entered={redirectPrompt.entered}
    target={redirectPrompt.target}
    busy={startPublicAudit.isPending}
    onConfirm={() => runAudit(redirectPrompt.target)}
    onCancel={() => setRedirectPrompt(null)}
  />
)}
```

- [ ] **Step 4: Typecheck + lint**

```bash
npx tsc --noEmit
npm run lint
```
Expected: no errors.

- [ ] **Step 5: Commit**

```bash
git add app/auditor/page.tsx
git commit -m "feat(auditor): public submit shows redirect-confirm modal, resubmits with confirmed_target"
```

---

## Task 5: Dashboard list-page wiring

**Repo:** `luran-dashboard`.
**Depends on:** T2, T3. **Parallel with:** T4, T6, T7.

**Files:**
- Modify: `app/(dashboard)/dashboard/grow/auditor/page.tsx` (`handleStart` ~168-203, `handleRescan` ~210-231, render tree)

**Interfaces consumed:** `getRedirectConfirmation`, `RedirectConfirmModal`, `useStartAudit` (with `confirmed_target`).

- [ ] **Step 1: Import helper + modal**

```tsx
import { getRedirectConfirmation } from "@/app/lib/redirect-confirm";
import { RedirectConfirmModal } from "@/app/components/luran/auditor/RedirectConfirmModal";
```

- [ ] **Step 2: Prompt state + a shared start function**

Add state:

```tsx
const [redirectPrompt, setRedirectPrompt] = useState<{ entered: string; target: string; sourceUrl: string } | null>(null);
```

(The `sourceUrl` remembers which URL to re-submit — `handleStart` uses the typed URL, `handleRescan` uses `audit.url`.) Add a shared runner:

```tsx
const runAudit = useCallback(
  (sourceUrl: string, confirmedTarget?: string) => {
    setSubmitError(null);
    startAudit(
      { url: sourceUrl, ...(confirmedTarget ? { confirmed_target: confirmedTarget } : {}) },
      {
        onSuccess: ({ audit_report_id }) => {
          setRedirectPrompt(null);
          router.push(`/dashboard/grow/auditor/${audit_report_id}`);
        },
        onError: (err) => {
          const confirmation = getRedirectConfirmation(err);
          if (confirmation) {
            setRedirectPrompt({ ...confirmation, sourceUrl });
            return;
          }
          setRedirectPrompt(null);
          if (isAxiosError(err) && err.response?.status === 403) {
            setUpgradeMessage(
              err.response?.data?.error?.message ??
                "You've reached your audit limit for this billing period. Upgrade your plan to run more audits.",
            );
          } else {
            setSubmitError("Audit could not be started. Check the URL and try again.");
          }
        },
      },
    );
  },
  [startAudit, router],
);
```

Rewrite `handleStart` to keep its client-side URL validation, then call `runAudit(trimmed)`; rewrite `handleRescan` to call `runAudit(audit.url)`.

- [ ] **Step 3: Render the modal**

```tsx
{redirectPrompt && (
  <RedirectConfirmModal
    entered={redirectPrompt.entered}
    target={redirectPrompt.target}
    busy={isStarting}
    onConfirm={() => runAudit(redirectPrompt.sourceUrl, redirectPrompt.target)}
    onCancel={() => setRedirectPrompt(null)}
  />
)}
```

Use whatever pending flag the page already exposes from `useStartAudit` for `busy` (grep for `isPending`/`isStarting`; if none is destructured, add it from the mutation hook).

- [ ] **Step 4: Typecheck + lint**

```bash
npx tsc --noEmit
npm run lint
```
Expected: no errors.

- [ ] **Step 5: Commit**

```bash
git add "app/(dashboard)/dashboard/grow/auditor/page.tsx"
git commit -m "feat(auditor): dashboard start/rescan show redirect-confirm modal, resubmit with confirmed_target"
```

---

## Task 6: Dashboard report page — rescan wiring + transparency line

**Repo:** `luran-dashboard`.
**Depends on:** T2, T3 (wiring); T1+T2 (transparency data). **Parallel with:** T4, T5, T7.

**Files:**
- Modify: `app/(dashboard)/dashboard/grow/auditor/[auditId]/page.tsx` (`handleRescan` ~140-160, `PageHeader` desc ~494-524)

- [ ] **Step 1: Import helper + modal**

```tsx
import { getRedirectConfirmation } from "@/app/lib/redirect-confirm";
import { RedirectConfirmModal } from "@/app/components/luran/auditor/RedirectConfirmModal";
```

- [ ] **Step 2: Wire the rescan submit**

Add prompt state:

```tsx
const [redirectPrompt, setRedirectPrompt] = useState<{ entered: string; target: string } | null>(null);
```

In `handleRescan`, on the mutation `onError`, add 409 handling before the existing branches:

```tsx
onError: (err) => {
  const confirmation = getRedirectConfirmation(err);
  if (confirmation) {
    setRedirectPrompt(confirmation);
    return;
  }
  setRedirectPrompt(null);
  // ...existing 403 / generic handling unchanged...
},
```

and re-submit on confirm using `audit.url` plus `confirmed_target`. Render the modal at the end of the JSX:

```tsx
{redirectPrompt && (
  <RedirectConfirmModal
    entered={redirectPrompt.entered}
    target={redirectPrompt.target}
    busy={/* the rescan mutation's isPending */ false}
    onConfirm={() => {/* call the rescan mutation with { url: audit.url, confirmed_target: redirectPrompt.target } */}}
    onCancel={() => setRedirectPrompt(null)}
  />
)}
```

Wire `busy` and `onConfirm` to the actual rescan mutation the file uses (grep the file for how `handleRescan` calls the mutation — mirror its arguments, adding `confirmed_target`).

- [ ] **Step 3: Transparency line in the header desc**

The header desc (~line 523) is built from `auditedHost = new URL(audit.url).hostname`. Add an entered-vs-scanned note and pages reached. Replace the desc expression with:

```tsx
const scannedHost = (() => {
  try {
    return new URL(audit.url).hostname;
  } catch {
    return audit.url;
  }
})();
const enteredHost =
  audit.entered_url && audit.entered_url !== audit.url
    ? (() => {
        try {
          return new URL(audit.entered_url).hostname;
        } catch {
          return audit.entered_url as string;
        }
      })()
    : null;
const scannedNote = enteredHost ? `${scannedHost} (you entered ${enteredHost})` : scannedHost;
const pagesNote =
  typeof audit.pages_scanned === "number" && audit.pages_scanned > 0
    ? ` · ${audit.pages_scanned} pages reached`
    : "";
```

and set:

```tsx
desc={`${scannedNote} · ${stats.total} entries extracted${pagesNote}. Choose what goes live — everything else is saved for later.`}
```

(`audit.entered_url` / `audit.pages_scanned` come from the `AuditListItem` fields added in T2, populated by T1.)

- [ ] **Step 4: Typecheck + lint**

```bash
npx tsc --noEmit
npm run lint
```
Expected: no errors.

- [ ] **Step 5: Commit**

```bash
git add "app/(dashboard)/dashboard/grow/auditor/[auditId]/page.tsx"
git commit -m "feat(auditor): dashboard report rescan-confirm modal + scanned-vs-entered transparency"
```

---

## Task 7: Public report view — transparency line

**Repo:** `luran-dashboard`.
**Depends on:** T2 (types); T1 for real data. **Parallel with:** T4, T5, T6.

**Files:**
- Modify: `app/components/luran/auditor/AuditReportView.tsx` (meta row ~338-352)

- [ ] **Step 1: Replace the meta row with accurate, transparent copy**

The current meta row reads `Free scan · first 50 pages · {report.pages_scanned} pages scanned` — the "first 50 pages" is wrong (public cap is 10) and there's no entered-vs-scanned note. Replace it:

```tsx
{(() => {
  const scannedHost = (() => {
    try {
      return new URL(report.url).hostname;
    } catch {
      return report.url;
    }
  })();
  const enteredHost =
    report.entered_url && report.entered_url !== report.url
      ? (() => {
          try {
            return new URL(report.entered_url as string).hostname;
          } catch {
            return report.entered_url as string;
          }
        })()
      : null;
  return (
    <>
      <span>{enteredHost ? `Scanned ${scannedHost} (you entered ${enteredHost})` : `Scanned ${scannedHost}`}</span>
      <span>·</span>
      <span>Free scan · up to 10 pages</span>
      <span>·</span>
      <span>{report.pages_scanned} pages reached</span>
    </>
  );
})()}
```

Keep the surrounding container markup/styles exactly as they are — only the inner spans change. (`report.url` / `report.entered_url` come from the `PublicAuditReport` fields added in T2, populated by T1.)

- [ ] **Step 2: Typecheck + lint**

```bash
npx tsc --noEmit
npm run lint
```
Expected: no errors.

- [ ] **Step 3: Commit**

```bash
git add app/components/luran/auditor/AuditReportView.tsx
git commit -m "feat(auditor): public report shows scanned-vs-entered host + honest 10-page copy"
```

---

## Task 8: Playwright e2e — 409 confirm + resubmit (public + dashboard)

**Repo:** `luran-dashboard`.
**Depends on:** T4, T5, T6, T7. **Parallel with:** none.

**Files:**
- Modify: `tests/e2e/auditor.spec.ts` (public stubs ~87-158; dashboard stubs `stubDashboardAuditApis` ~350-529)

- [ ] **Step 1: Public 409 → modal → resubmit test**

Add a test that route-mocks `POST **/api/v1/public/audit`: the FIRST call returns 409 with body `{ success:false, error:{ code:"REDIRECT_CONFIRMATION_REQUIRED", message:"...", details:{ entered_url:"https://oldco.com", redirect_target:"https://newco.io/" } } }`; the SECOND call returns 202 success. Assert:
- After submitting `oldco.com`, the confirm modal appears containing `oldco.com` and `newco.io`.
- Clicking "Yes, scan it" fires a second POST whose `postDataJSON()` contains `confirmed_target: "https://newco.io/"` and the app transitions to scanning.

Use the existing "count calls / capture postData" idiom already in the file (`expect.poll(() => postCount)`, `route.fulfill(...)`), and the existing 429 test (lines ~145-158) as the structural template.

- [ ] **Step 2: Dashboard 409 test**

In the dashboard flow (extend `stubDashboardAuditApis`, `POST **/api/v1/audit` ~418-438), add the same first-409-then-202 sequence; assert the modal appears and the confirmed resubmit carries `confirmed_target`, then the app navigates to `/dashboard/grow/auditor/<id>`.

- [ ] **Step 3: Run the e2e suite**

```bash
npm run build
npm run test:e2e -- auditor.spec.ts
```
(or run against a live `npm run dev` with `PLAYWRIGHT_BASE_URL=http://localhost:3000 npm run test:e2e -- auditor.spec.ts`.) Expected: the new tests pass; do not regress existing auditor specs.

- [ ] **Step 4: Commit**

```bash
git add tests/e2e/auditor.spec.ts
git commit -m "test(auditor): e2e redirect-confirm modal + confirmed_target resubmit (public + dashboard)"
```

---

## Self-Review (author)

- **Spec coverage:** confirm modal on both surfaces (T4 public, T5 dashboard list, T6 dashboard report) consuming the 409 and re-submitting `confirmed_target` (T2 hooks + T3 helper/modal); transparency "scanned vs entered · pages reached" on public (T7) and dashboard (T6) reports; backend exposure of `url`/`entered_url` (T1). e2e proof (T8).
- **Type consistency:** `getRedirectConfirmation → { entered, target } | null` (T3) consumed identically in T4/T5/T6; `RedirectConfirmModal` props `entered/target/busy/onConfirm/onCancel` (T3) used identically in T4/T5/T6; `confirmed_target` optional mutation var (T2) used by T4/T5/T6; `PublicAuditReport.url/entered_url` (T2) used by T7; `AuditListItem.entered_url/pages_scanned` (T2) used by T6.
- **Verification honesty:** FE has no unit harness; per-task checks are `tsc --noEmit` + `eslint`, behavior is proven by the Playwright task (T8). This is stated in the Verification model, not hidden.
- **Implementer verification points (explicit, not placeholders):** `Btn` `disabled` support (T3 Step 2); the report GET's `PublicReportResponse(` construction site (T1 Step 2); the dashboard rescan mutation's argument shape and pending flag (T5 Step 3, T6 Step 2). Each names exactly what to grep and how to adapt.

## Parallel execution note

If executing subagent-driven, dispatch each wave's tasks concurrently ONLY after confirming their file sets are disjoint (they are, per the File Structure map). Review each task individually as it returns. Do not dispatch a later wave until the current wave is reviewed and its commits are on the branch, because Wave 2 imports the symbols Wave 1 creates.
