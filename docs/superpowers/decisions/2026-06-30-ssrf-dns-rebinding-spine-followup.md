# Follow-up: spine-wide SSRF DNS-rebinding hardening (outbound webhooks)

**Date:** 2026-06-30
**Status:** RESOLVED 2026-10-14 — implemented in `fix/ssrf-pinned-outbound` via `app/utils/pinned_http.py` (see `docs/sop/ssrf_pinned_outbound.md`). Original record below. Note: `send_slack_notification` had no gate at all (not only a TOCTOU-prone one); it is now pinned and fail-closed.
**Severity:** Important (security, defense-in-depth) — not a merge blocker for 5e
**Owner:** unassigned

## Context

Stage 3 Slice 5e added the CRM webhook channel. Its ARQ job (`sync_lead_to_crm` in `app/workers/sales_notifications.py`) validates the tenant-configured webhook URL with `app.utils.network_security.ensure_safe_outbound_url(url)` before POSTing — this blocks direct/static SSRF (non-http(s) schemes; hosts resolving to loopback/private/link-local/multicast/metadata IPs such as `169.254.169.254`). That closed the MEDIUM SSRF flagged by automated commit review.

## The residual gap

`ensure_safe_outbound_url` is a **yes/no gate**: it resolves the hostname, validates the IPs, then returns. The subsequent `httpx.Client.post(url, ...)` resolves the hostname **again, independently**, and connects by name. A tenant who controls DNS for their own webhook host can pass the check (return a public IP) and then **rebind** the hostname to `169.254.169.254` / an RFC1918 address for the actual connection (TOCTOU / DNS-rebinding). It is blind SSRF (the response body is not surfaced to the tenant; only a best-effort `crm_id` is extracted), and the attacker must be a tenant admin controlling their own DNS — but the metadata/internal-service block is bypassable under rebinding.

The util itself documents this: its docstring says *"New code should prefer `resolve_and_validate_host` + pinning"* and that `ensure_safe_outbound_url` is "kept for callers that only need a yes/no gate."

## Why it was deferred (not fixed in 5e)

The gap is **not specific to CRM** — it is the same posture used by:
- `app/workers/sales_notifications.py::send_slack_notification` (already merged, 5d)
- `app/services/flow_engine.py:644` (action-call outbound)
- `app/services/url_scraper_service.py` (outbound scrape)

5e faithfully implements the spec's chosen guard and matches existing usage, so fixing only the CRM job would make it inconsistently stricter than its siblings while leaving the same gap in already-merged code. **Decision (user, 2026-06-30): ship 5e as-is; file this spine-wide follow-up.**

(`httpx.Client` defaults `follow_redirects=False`, so redirect-based SSRF bypass is already mitigated — a 3xx falls through to the FAILED-no-retry branch. Only the rebinding/TOCTOU vector remains.)

## Proposed work (do once, across the spine)

Migrate every tenant-controlled outbound request to **`resolve_and_validate_host(hostname)` + connect-by-pinned-IP**, honoring the util's connection contract: connect to the returned IP while sending the ORIGINAL hostname as the `Host` header and TLS SNI / cert-verification name (do not splice the IP into the URL host — that breaks TLS and drops the Host header; bracket IPv6). The auditor crawler/discovery already do this (`app/auditor/crawler.py`, `app/auditor/discovery.py`) — reuse that pattern.

Targets:
- `sync_lead_to_crm` (CRM webhook)
- `send_slack_notification` (Slack webhook)
- `flow_engine.py` action-call outbound
- `url_scraper_service.py`

Fixing it once covers slack/crm/url_scraper/flow_engine together.

## Related minor follow-ups surfaced in the 5e final review (non-blocking)

- **Defense-in-depth tenant match:** `sync_lead_to_crm` loads the `Lead` by PK (`db.get(Lead, log.entity_id)`) without re-checking `lead.tenant_id == log.tenant_id`. Safe today (dispatch is tenant-scoped); a one-line guard would harden against any future cross-tenant `entity_id` writer.
- **Blocking sync I/O in async jobs:** `socket.getaddrinfo` (in the guard) and the sync `httpx.Client.post` (≤10s) run inside `async def` ARQ jobs, stalling other coroutines on that worker. Identical to the merged `send_slack_notification` — a spine-level async-client / thread-executor follow-up, not a 5e item.
