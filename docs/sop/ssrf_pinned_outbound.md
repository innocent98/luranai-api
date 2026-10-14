# SOP — Pinned outbound connections for tenant/user-supplied URLs

**Status:** Shipped (PR to `develop`) · `fix/ssrf-pinned-outbound` · 2026-10-14
**Area:** `app/utils/pinned_http.py` (new), `app/utils/network_security.py`, `app/workers/sales_notifications.py`, `app/services/flow_engine.py`, `app/services/url_scraper_service.py`, `app/services/automation_service.py`, `app/auditor/{crawler,discovery}.py`
**Closes:** `docs/superpowers/decisions/2026-06-30-ssrf-dns-rebinding-spine-followup.md`

## What shipped

One shared helper, `app.utils.pinned_http`, now performs every outbound HTTP request whose URL a tenant or end user controls. The hostname is resolved once, every answer is validated, and the socket connects to the validated IP. A hostname that re-resolves to a private/link-local address after the check is never connected to.

## Why

`ensure_safe_outbound_url` was a yes/no gate: it resolved and validated, then httpx resolved the name again and connected by name (check/connect TOCTOU). A tenant controlling DNS for their webhook host could pass the check and rebind to `169.254.x` or RFC1918 space (blind SSRF). The auditor and the automation service already pinned, but each had its own copy of the logic and the rest of the spine did not pin at all.

## How

`app/utils/pinned_http.py`:

| Function | Purpose |
|---|---|
| `build_pinned_request(method, url, safe_ip, ...)` | Pure builder: URL host replaced by the IP, `Host` header = original host(+port, never userinfo), `sni_hostname` extension = original host so TLS SNI and certificate verification use the hostname. Allowlisted hosts connect by name. |
| `resolve_pinned_ips(url, allowed_hostnames)` | Scheme/host check + `resolve_and_validate_hosts` (all validated answers, **IPv4 first**). Policy block -> `UnsafeOutboundURLError` (a `ValueError`); DNS failure -> `OutboundResolutionError` (not a block, retryable). |
| `apinned_request(...)` / `pinned_request(...)` | Async / sync request. DNS runs in a thread in the async variant (no event-loop blocking). On `ConnectError`/`ConnectTimeout` the next validated IP is tried (max 4), restoring the happy-eyeballs behaviour of connect-by-name on hosts with no IPv6 route. Body is streamed with a byte cap (`max_bytes`, default 1 MB; scraper 10 MB -> `ResponseTooLargeError`) and an overall deadline (`total_timeout`, default 30 s). A fresh client/connection is used per hop, so a redirect to another hostname never reuses a pooled connection. `max_redirects=0` by default (3xx is returned, not followed). |
| `original_url(response)` | `response.url` carries the IP; this returns the URL the caller asked for. |

Redirects: callers that opt in (`max_redirects=N`, used by the URL scraper) get a full re-validate + re-pin on every hop; `Authorization`/`Cookie` are dropped on cross-origin hops; 303 (and 301/302 on POST) downgrade to GET without a body; scheme must stay http(s); exceeding N raises `UnsafeOutboundURLError("Too many redirects")`; an https->http downgrade is refused.

`network_security._is_disallowed_ip` was hardened: any non-globally-routable address is rejected (adds CGNAT `100.64/10`), and IPv6 forms embedding IPv4 (IPv4-mapped, NAT64 `64:ff9b::/96`, 6to4, Teredo) are judged by the IPv4 they carry.

Alternatives rejected: a custom httpx transport that overrides name resolution (keeps URL/Host handling implicit but hides the pin and makes per-hop redirect validation harder); `curl_cffi resolve=` (only the auditor's browser-impersonating fetch needs that engine).

## What's involved

| Path | Change |
|---|---|
| `app/workers/sales_notifications.py` `send_slack_notification`, `sync_lead_to_crm` | Sync `httpx.Client.post` + gate replaced by `apinned_request`. Slack previously had **no** SSRF gate; it now fails closed with `log.error = "blocked webhook host"` (no ARQ retry), same as CRM. Jobs no longer block the worker loop on DNS/HTTP. |
| `app/services/flow_engine.py` `_exec_webhook` | `apinned_request`; "Unsafe webhook URL: ..." error string unchanged; unsupported-method check moved before the call. |
| `app/services/url_scraper_service.py` | Manual redirect loop replaced by `apinned_request(max_redirects=5)`; returned `url` is the original/final URL, not the IP. |
| `app/services/automation_service.py` `_build_pinned_request` | Delegates to `build_pinned_request` (behaviour identical; also strips caller-supplied `Host`). |
| `app/auditor/crawler.py`, `discovery.py` | Three copy-pasted pin blocks replaced by `build_pinned_request`. curl_cffi (`resolve=`) path unchanged (already pinned). |
| `app/auditor/crawler.py` headless browser route handler | See "Auditor browser" below. |

Audited and out of scope (fixed vendor endpoints, no tenant/user URL): Paystack, FCM, ElevenLabs, SMTP, Cloudinary. `source_url` on knowledge documents is stored, never fetched. No migrations, no config, no API shape changes.

## Resource bounds (where caps and deadlines are applied)

| Bound | Where it is enforced |
|---|---|
| Byte cap | While streaming, on wire bytes AND decoded bytes. zlib is driven with `max_length`, so a gzip/deflate bomb never materialises more than the cap (tested with tracemalloc). `Content-Length` over the cap is refused before reading. Only `gzip, deflate` are advertised; any other `Content-Encoding` is rejected (`UnsupportedContentEncodingError`). |
| Redirect bodies | A redirect that is followed is closed unread; one that is returned to the caller is read under the cap. |
| Total deadline | Async: one `asyncio.timeout` around DNS, every fallback IP, every hop and the body (not reset per hop or IP). Sync: checked before each lookup/attempt/hop and between body chunks, with per-operation timeouts clipped to the time left (max IPs x per-IP timeout can no longer exceed it). |
| Rejected bodies | `ResponseRejectedError` (too large / undecodable) is permanent: CRM and Slack mark the log FAILED (`response rejected`) without an ARQ retry. |
| Tenant actions | `AutomationService._attempt_request` now reads through `aread_capped` (5 MB). |

## Auditor browser (Chromium)

The old route handler validated other hosts with our DNS and then `route.continue_()`d, so Chromium re-resolved them by name: a scanned page's JS could `fetch()` a rebinding host that flipped to an internal address. Two layers now guarantee Chromium never connects by name to an unpinned host:

1. **Resolver rule** `--host-resolver-rules=MAP <root> <validated-ip>, MAP * ~NOTFOUND`. Redirects Chromium follows itself after `continue_()`, IP-literal URLs and WebSockets are NOT visible to `page.route`, so they are stopped here: Chromium cannot resolve anything but the root host. (`EXCLUDE localhost` was dropped: it would have left `localhost` resolvable.) Probe in `docs/captures/ssrf-pinned-outbound/chromium-resolver-probe.txt`: root 302 to an IP literal or to another name fetches nothing, and a WebSocket to a loopback IP fails.
2. **Route handler** for what the resolver rule cannot express (cross-host subresources must still load):

| Request | Handling |
|---|---|
| image / media / font / stylesheet | aborted (unchanged) |
| non-http(s) | aborted (unchanged) |
| pinned root host | `continue_()` (pinned by Chromium) |
| other host, GET `script` / `xhr` / `fetch` | fetched through `apinned_request` (validated, pinned, redirects followed and re-validated per hop inside the helper so no `Location` is handed back to Chromium, 5 MB cap, 15 s) and returned via `route.fulfill()` |
| other host, anything else (documents/iframes, WebSocket, POST, ...) | aborted |

Tradeoffs: third-party scripts and cross-host API calls (CDN bundles, `api.` subdomains) still load, so SPA hydration and audit quality are kept; cross-host iframes, WebSockets and POSTs are dropped, as is a main-frame navigation that leaves the root host mid-crawl (the crawl is same-host only). Live run of the real crawler on a multi-host public site (`chromium-live-audit.json`): python.org rendered (200, 53 KB) with 5 cross-host scripts fulfilled via the pinned fetch; en.wikipedia.org rendered (200, 413 KB). Fulfilled responses are fully buffered (the 5 MB cap bounds memory) and each costs one DNS lookup plus a fresh connection. The old per-crawl host-validation cache is gone, since nothing is continued by name any more.

## Verification

- New tests: `tests/unit/test_pinned_http.py` (builder, rebinding sync+async, mixed answers, IPv6/IPv4-mapped/NAT64/6to4/CGNAT, unresolvable host, redirects per hop, credential stripping, 303, real local TLS server proving the cert is checked against the hostname while connecting to the IP), `tests/services/test_flow_webhook_pinning.py`, `tests/services/test_url_scraper_pinning.py`, rebinding/redirect/blocked cases added to `tests/workers/test_crm_sync.py` and `test_slack_notification.py`, address-class cases in `tests/unit/test_network_security.py`. Existing worker tests now patch `httpx.AsyncClient.send`.
- Rebinding tests: resolver answers public on the first lookup and `169.254.169.254` afterwards; the assertion is that the request handed to the transport targets the public IP and that the name was resolved exactly once.
- Live (real DNS/network, throwaway Postgres): `docs/captures/ssrf-pinned-outbound/` holds `baseline-origin-develop.json` and `fixed-pinned-outbound.json`. Highlights: CRM job to `https://httpbin.org/post` -> `sent`; to `http://169.254.169.254.nip.io/...` -> `blocked webhook host`, zero TCP connects; simulated rebinding of `httpbin.org` on `origin/develop` -> TCP connect to `169.254.169.254`, on this branch -> `sent`, only the validated public IP contacted; redirect from a public host to the metadata IP is stopped at the hop.

## Operate / roll back

Pure code change; revert the PR. No data or config to unwind. Behaviour deltas to know: A DNS outage on a CRM/Slack host is now retried by ARQ (previously recorded as a permanent block). Slack webhooks to non-public hosts now fail with `blocked webhook host`; CGNAT and embedded-IPv4 IPv6 targets are now blocked everywhere `resolve_and_validate_host` is used (including the auditor).

## Follow-ups

- `ensure_safe_outbound_url` is now unused by app code; keep or delete in a separate cleanup.
- `AutomationService.execute` still calls the blocking `resolve_and_validate_host` directly inside an async path (it already pinned; wrap in `asyncio.to_thread`).
- `sync_lead_to_crm` still does not re-check `lead.tenant_id == log.tenant_id` (from the 2026-06-30 record).
