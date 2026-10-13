# SOP — Real client IP behind Cloudflare → nginx → Docker (rate limits)

**What shipped:** `FORWARDED_ALLOW_IPS` on the `api` service (`docker-compose.prod.yml`, used by both
staging and prod CD) + a Cloudflare `real_ip` conf in host nginx (manual, root). Together they make
`request.client.host` the real visitor IP. Branch `fix/trust-proxy-client-ip`.

## Why
Every IP-keyed limiter (`app/utils/http_rate_limiter.py:167-174`, `public_auditor.py:101`,
`widget.py:430`) reads `request.client.host`. Staging logs showed **every** client as `172.19.0.1`
(the compose network gateway) → all visitors on all tenants shared ONE bucket. Blocker for N4 prod:
the public rating limit (20/h/IP) also gates the legacy rating POST every live widget uses.

Two hops each hid the IP:

| Hop | Saw | Fix |
|---|---|---|
| Cloudflare → nginx | Cloudflare edge IP as `$remote_addr` (DNS for api/staging-api is proxied, `server: cloudflare`) | nginx `real_ip_header CF-Connecting-IP` + `set_real_ip_from` Cloudflare ranges |
| nginx → container | Docker gateway `172.18.0.1` (prod) / `172.19.0.1` (staging); uvicorn trusted XFF only from 127.0.0.1 | `FORWARDED_ALLOW_IPS=172.16.0.0/12` |

## How / decisions
- uvicorn 0.32.1 `ProxyHeadersMiddleware` (reached via gunicorn `UvicornWorker`, which passes
  gunicorn's `forwarded_allow_ips`, default from the `FORWARDED_ALLOW_IPS` env) supports CIDR and takes
  the **rightmost untrusted** XFF entry → client-supplied XFF cannot spoof. Verified locally on the
  pinned version: normal → visitor IP; `6.6.6.6, real, real` → real; untrusted peer → peer.
- CIDR `172.16.0.0/12` rather than the exact gateway: compose network subnets can change if a network
  is recreated; only the host and other compose containers can reach the 127.0.0.1-bound port.
- `set_real_ip_from` restricted to Cloudflare's published ranges → a direct-to-origin request can't
  forge `CF-Connecting-IP`. Existing `X-Forwarded-For $proxy_add_x_forwarded_for` lines unchanged.
- Rejected: reading `CF-Connecting-IP` in app code (spoofable on direct-to-origin hits).

## Operate
nginx step (root on VPS, once; re-run to refresh Cloudflare ranges):
```bash
{ echo "real_ip_header CF-Connecting-IP;"; for v in 4 6; do curl -s https://www.cloudflare.com/ips-v$v | sed 's/.*/set_real_ip_from &;/'; done; } | sudo tee /etc/nginx/conf.d/cloudflare-realip.conf
sudo nginx -t && sudo systemctl reload nginx
```
Order: either order is safe; with only the app change, client.host becomes a Cloudflare edge IP.

## Verification
- After both land: `docker logs --since 5m luran_staging_api | grep -v 172.1` — gunicorn access log
  shows public visitor IPs, not `172.19.0.1`. Two networks → two distinct `http_rating:*` Redis keys.

## Roll back
Remove `/etc/nginx/conf.d/cloudflare-realip.conf` + reload; set `FORWARDED_ALLOW_IPS=127.0.0.1` in the env
(or revert the compose line) and redeploy.

## Follow-ups
- nginx config is not in git (tracked for the AWS/IaC migration).
- Cloudflare ranges change rarely; refresh the conf when they do.
