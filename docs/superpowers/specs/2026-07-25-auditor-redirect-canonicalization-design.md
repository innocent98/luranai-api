# Auditor Redirect Canonicalization — Design

**Date:** 2026-07-25
**Status:** Proposed (awaiting review)
**Owner area:** `app/auditor` + auditor submit endpoints + dashboard/public auditor FE

## Problem

Auditing the **apex** `luranai.com` returned 3 entries and 7 false "gaps" for
content that exists on the site. Root cause, verified with live requests:

```
https://luranai.com             → 307 → https://www.luranai.com/
https://luranai.com/sitemap.xml → 307 → https://www.luranai.com/sitemap.xml
```

The apex 307-redirects to `www`. The auditor does not canonicalize host
redirects, and it fails in two compounding ways:

1. **Sitemap seeding dies.** `discovery._default_fetch_text` uses
   `follow_redirects=False` (a deliberate SSRF guard) and returns text only on
   HTTP 200. The 307 → `None`, so all 19 sitemap URLs are discarded; only the
   root is seeded.
2. **The crawl's own links are filtered out.** The render follows the redirect
   to `www`, but same-host filtering is against the entered **apex** host, so
   every `www.` link is dropped as off-host. Only paths that resolve back to the
   apex survive → home, pricing, privacy.

This is not stale code — `main` (production) has every prior auditor fix. It is a
genuine gap: **the auditor does not follow host-canonicalization redirects.**
apex↔www and http→https are among the most common web configs, so this silently
degrades audits for a large fraction of real sites, and does so *invisibly* —
the user gets a confident, wrong result rather than an error.

## Goals

- An audit of `luranai.com` reaches the whole site (fixes the reported bug).
- Handle the common redirect class (apex↔www, scheme upgrade) with **no user
  friction**.
- For redirects that land on a host the user would **not** recognize as their
  own site (different subdomain, different domain), get **explicit consent
  before scanning** — never silently scan a target the user did not enter.
- Preserve every existing SSRF / DNS-rebinding invariant. Following a redirect
  must re-validate each hop; it must not reopen blind-redirect SSRF.
- Be transparent: the result shows the URL actually scanned when it differs from
  what was entered.

## Non-goals

- Changing luranai.com's own apex→www redirect (a legitimate SEO choice; the fix
  belongs in the auditor, which must handle every customer's redirects).
- A general open-redirect follower or external-site scanner.
- Changing in-crawl link following (Playwright already follows per-page
  redirects during render; canonicalizing the **root** is sufficient because
  seeding and same-host filtering both key off the root).

## Design: tiered redirect handling

At submit time, before any report/job is created, resolve the entered root's
redirect chain **safely and bounded**, then classify by where it lands.

### The safe redirect resolver

A new helper (in `app/auditor/discovery.py` or `app/utils/network_security.py`)
resolves the final URL of the entered root:

```
resolve_redirect_chain(url, max_hops=3) -> ResolvedRoot
  ResolvedRoot = { final_url, hops: list[str], changed: bool }
```

Algorithm, per hop:
1. Parse; reject non-http(s) scheme → `ValueError` (block).
2. `resolve_and_validate_host(host)` — SSRF/private-range guard, pin the IP.
   Any `ValueError` (incl. `HostResolutionError`) → block (Tier 3).
3. Issue a GET with `follow_redirects=False`, connection pinned to the validated
   IP, original Host header + SNI (identical machinery to `_default_fetch_text`).
4. On 3xx with a `Location`: resolve `Location` against the current URL, then
   loop (re-guarding the new host from scratch — never reuse a prior pinned IP,
   never trust the `Location` host without re-validating).
5. On 2xx: stop; this is `final_url`.
6. Exceeding `max_hops`, or a redirect loop → `ValueError("too many redirects")`
   (block, surfaced as an actionable 400).

**Security invariant:** every hop is a fresh SSRF decision. `max_hops` is a hard
cap. This helper is the ONLY place that follows redirects; the crawl fetch stays
`follow_redirects=False`.

### Tier classification

Let `H_in` = entered host, `H_final` = final host (after the chain). Compare with
`www.` stripped and lowercased on both sides.

- **Tier 1 — same site (follow silently):** `H_final` equals `H_in` after
  stripping a leading `www.` from either, with only a scheme upgrade
  (`http`→`https`) otherwise. Examples: `luranai.com`→`www.luranai.com`,
  `www.x.com`→`x.com`, `http://x.com`→`https://x.com`. Also the no-redirect case
  (`H_final == H_in`). Proceed; the report's scanned URL becomes `final_url`.
- **Tier 2 — different host (confirm first):** any other host change — different
  subdomain (`x.com`→`app.x.com`), different domain (`x.com`→`y.com`), or a chain
  ending elsewhere. Do **not** create a report. Return a structured
  "confirmation required" response naming the target.
- **Tier 3 — unsafe (block):** any hop fails the SSRF guard, non-http(s), or the
  chain loops/exceeds `max_hops`. Reject with a 400 (reuse the existing
  "can't be audited" copy for the unsafe case).

Rationale for Tier 1 being silent rather than confirmed: apex↔www is the *same
registrable site's* canonical address and the single most common redirect on the
web. Prompting there trains confirm-fatigue, which devalues the Tier 2 prompt
that actually carries a security/consent signal.

## Submit / confirm contract

Both `public_auditor.start_public_audit` and dashboard `auditor` submit funnel
through `AuditService.start_audit`. Changes:

`start_audit(..., confirmed_target: str | None = None)`:

- **No `confirmed_target` (first submit):**
  - Run `_assert_target_auditable` (unchanged), then `resolve_redirect_chain`.
  - Tier 1 / no-redirect → build the report with `url = final_url`,
    `entered_url = normalized` (see Transparency), enqueue as today.
  - Tier 2 → raise `RedirectConfirmationRequired(entered=normalized,
    target=final_url)`. No rows created.
  - Tier 3 → `ValueError` (existing 400 path).
- **With `confirmed_target` (second submit, after the user clicked confirm):**
  - Re-run `resolve_redirect_chain` on the entered url **server-side** and
    compare its `final_url` to `confirmed_target`.
  - Match → proceed (`url = final_url`). **Never trust the client's target
    blindly** — it is only an assertion of what the user was shown.
  - Mismatch (redirect changed between calls) → raise
    `RedirectConfirmationRequired` again with the *new* target. Do not scan.

Endpoint mapping for `RedirectConfirmationRequired`: HTTP **409** with body
`{ code: "REDIRECT_CONFIRMATION_REQUIRED", entered_url, redirect_target,
message }`. Request schema on both endpoints gains an optional
`confirmed_target: string`.

### Rate-limit interaction (public tool)

The public rate limiter is a dependency that fires before the endpoint, so a
`confirmation_required` round-trip currently burns a scan credit without a scan.
**Design decision:** the confirmation-required response must **not** consume a
scan credit — only an enqueued audit should. Either decrement the credit
post-enqueue, or exempt 409/400 responses from the limiter. Implementation task
will pick the least invasive of the two; flagged here so it is not missed.

## Transparency

Add a nullable `AuditReport.entered_url` column (migration). When `entered_url`
is non-null and differs from `url`, the FE shows: *"Scanned www.luranai.com (you
entered luranai.com) · N pages reached."* When null, entered == scanned; no
extra copy. This also covers Tier 2 confirmed cross-domain scans without
surprise.

**Pages-reached in the UI:** surface `pages_scanned` in the result header
regardless of redirect. This is the general "self-auditing audit" that helps in
every case, not just redirects — it is what would have made the original problem
obvious immediately.

## Honest thin-crawl safety net

Keep (and rely on) the existing scan-quality states. If, even after
canonicalization, the crawl reaches very few pages, the report must show a
thin/limited state rather than a confident score. This is the real defense
against "confident wrong answer," and it guards causes we have not yet found.

## Edge cases

- **No host change, path-only redirect** (`/` → `/en`): host equal → Tier 1,
  silent. Correct.
- **Ports / IDN / trailing slash:** compare normalized origins (scheme + host +
  port), `www.`-stripped. Punycode-normalize IDN before comparison.
- **Chain apex→www→https:** resolver walks to the final; classify on the end.
- **Redirect to a genuinely different domain the user DID intend** (`oldco.com`
  → `newco.io`): Tier 2 confirm → user accepts → scanned with transparency.
- **Redirect changes between confirm and re-submit:** server re-resolves and
  re-prompts; never scans an unconfirmed target.

## Testing strategy

- **Unit — classifier:** www toggle & scheme upgrade → Tier 1; subdomain and
  cross-domain → Tier 2; internal-IP hop → Tier 3; loop / >max_hops → Tier 3.
- **Unit — resolver security:** a hop to `169.254.169.254` (literal, no DNS) is
  blocked; each hop re-guards (a chain public→internal is blocked at the
  internal hop).
- **Integration — start_audit** (mocked resolver):
  - apex→www → report created with `url = www…`, `entered_url = apex`, no
    confirmation.
  - cross-domain → `RedirectConfirmationRequired`, no rows created.
  - second call with matching `confirmed_target` → proceeds.
  - second call with a `confirmed_target` that no longer matches → re-prompts,
    no scan.
- **Regression (the reported bug):** seeding from the canonical `www` host
  yields all sitemap `<loc>` URLs and same-host filtering keeps them.
- Endpoints return 409 with the documented body for Tier 2.

## Rollout / backward-compat

- `entered_url` is additive and nullable; existing reports read as
  "entered == scanned."
- `confirmed_target` is an optional request field; existing clients that never
  send it get Tier 1 auto-follow and Tier 2 as a 409 they can start handling
  when the FE modal ships. Backend can land before the FE modal; until the modal
  exists, Tier 2 audits surface an actionable 409 message instead of silently
  scanning the wrong host — strictly better than today.
- No change to the crawl fetch's `follow_redirects=False`.

## Decisions (resolved in review)

1. **Tier 1 silent vs. confirm — DECIDED: silent** for `www`/scheme toggle.
   Confirm-fatigue would devalue the Tier 2 prompt.
2. **Rate-limit credit — DECIDED:** a Tier 2 confirmation round-trip must not
   spend a scan credit; only an enqueued audit does. Implementation picks the
   least invasive mechanism.
3. **Cross-domain in the public tool — DECIDED: confirm + transparency only; do
   NOT refuse or specially throttle cross-domain.** Rationale: the public tool
   already permits scanning any public domain by typing it directly, so a
   cross-domain redirect grants no new capability. Refusal would break
   legitimate rebrands / vanity domains / agency audits for near-zero security
   gain. Internal-target hops remain blocked (Tier 3). Required hardening:
   - Keep a **generous per-IP anti-abuse limiter** distinct from the 3/day scan
     credit; every submit attempt (incl. confirm round-trips and the redirect
     pre-flight) passes through it, so the pre-flight cannot be used as an
     unmetered redirect/SSRF probe.
   - Block/error messages stay non-revealing ("can't be audited") — never echo a
     resolved internal IP, so the pre-flight is not an internal-host oracle.
   - Log `entered → final` for abuse review.
