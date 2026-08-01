# Auditor Bot-Protection Resilience Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the website auditor survive bot-protected and split-host sites — crawl cross-`www`/apex sitemap URLs, retry soft-blocks, and report an honest `BLOCKED` state instead of a false "couldn't reach your website."

**Architecture:** Four small units in `app/auditor/`: (1) a `canonicalize_to_root` URL rewrite applied in seeding and link-discovery; (2) 403/429 detection + bounded retry in `render_page`; (3) a new `ScanQuality.BLOCKED` state driven by a new `RawPageError.SOFT_BLOCKED`; (4) a realistic UA on a shared browser context so challenge-clearance cookies carry across pages. Each is independently testable; the existing SSRF guard, `follow_redirects=False` crawl fetch, and page budget are untouched.

**Tech Stack:** Python 3.11, FastAPI, SQLAlchemy, Alembic, Playwright (async Chromium), pytest.

## Global Constraints

- Commits in Luran repos MUST NOT include a Claude co-author trailer.
- API stays additive on `/api/v1` — no breaking changes (3 clients on v1).
- SSRF guard (`resolve_and_validate_host`, per-hop, IP-pinned, fail-closed) is preserved on every fetch/render path; no URL is rendered or fetched without passing the existing `guard_url` / route guard.
- The crawl fetch stays `follow_redirects=False`; `resolve_redirect_chain` remains the only redirect follower. This plan adds NO new redirect-follower.
- Design source of truth: `docs/superpowers/specs/2026-07-28-auditor-bot-protection-resilience-design.md`.

## Testing Environment

The host has no app dependencies; all tests run inside the `luran-api:latest` image. From the repo root (`/Users/adebayovictor/Documents/dev/luran/luran`):

```bash
docker run --rm -v "$PWD":/app -w /app -e PYTHONPATH=/app \
  -e SECRET_KEY=0123456789012345678901234567890123456789 \
  -e DATABASE_URL=postgresql://x:x@localhost/x -e REDIS_URL=redis://localhost:6379/0 \
  luran-api:latest python -m pytest <TEST_PATH> -v
```

The auditor unit tests touched here (`discovery`, `crawler`, `scan_quality`) are pure — no live DB/Redis needed. Wherever a step says `Run: pytest <path>`, use the wrapper above.

## File Structure

| File | Responsibility | Tasks |
|---|---|---|
| `app/auditor/discovery.py` | `canonicalize_to_root` helper; apply in `seed_urls` | 1 |
| `app/auditor/crawler.py` | apply canonicalize in `crawl_site`; `SOFT_BLOCKED` + retry in `render_page`; shared UA context | 1, 2, 4 |
| `app/core/config.py` | new auditor settings | 2, 4 |
| `app/db/models/audit.py` | `ScanQuality.BLOCKED` | 3 |
| `alembic/versions/<rev>_add_blocked_scan_quality.py` | additive enum value | 3 |
| `app/auditor/scan_quality.py` | `BLOCKED` classification + reason | 3 |
| `app/auditor/pipeline.py` | treat `BLOCKED` as a fail-with-reason state | 3 |
| `tests/auditor/test_canonicalize.py` (new) | Unit 1 tests | 1 |
| `tests/auditor/test_crawler.py` | retry + UA tests | 2, 4 |
| `tests/auditor/test_scan_quality.py` | BLOCKED classification tests | 3 |

---

### Task 1: Split-host URL canonicalization

**Files:**
- Modify: `app/auditor/discovery.py` (add `canonicalize_to_root`; use it in `seed_urls`, ~line 346-351)
- Modify: `app/auditor/crawler.py` (use it in `crawl_site` link loop, ~line 506-511)
- Test: `tests/auditor/test_canonicalize.py` (new)

**Interfaces:**
- Produces: `discovery.canonicalize_to_root(url: str, root_host: str) -> str` — rewrites `url`'s host to `root_host` when they are the same registrable host (differ only by a leading `www.`) but `url`'s netloc is not already `root_host`; otherwise returns `url` unchanged. Scheme/path/query preserved. Never raises.
- Consumes: existing `discovery._same_registrable_host(a, b) -> bool`, `discovery.normalize_url`, `discovery._extract_links`.

- [ ] **Step 1: Write the failing test**

Create `tests/auditor/test_canonicalize.py`:

```python
"""Tests for discovery.canonicalize_to_root — split-host (www/apex) rewrite."""

import pytest

from app.auditor import discovery
from app.auditor.discovery import canonicalize_to_root


def test_apex_loc_rewritten_to_www_root():
    assert canonicalize_to_root("https://luranai.com/pricing", "www.luranai.com") == \
        "https://www.luranai.com/pricing"


def test_www_loc_rewritten_to_apex_root():
    assert canonicalize_to_root("https://www.luranai.com/faq", "luranai.com") == \
        "https://luranai.com/faq"


def test_identical_host_unchanged():
    assert canonicalize_to_root("https://www.luranai.com/x", "www.luranai.com") == \
        "https://www.luranai.com/x"


def test_different_subdomain_unchanged():
    assert canonicalize_to_root("https://app.luranai.com/x", "www.luranai.com") == \
        "https://app.luranai.com/x"


def test_different_domain_unchanged():
    assert canonicalize_to_root("https://evil.com/x", "www.luranai.com") == \
        "https://evil.com/x"


def test_path_and_query_preserved():
    assert canonicalize_to_root("https://luranai.com/a/b?x=1", "www.luranai.com") == \
        "https://www.luranai.com/a/b?x=1"


@pytest.mark.asyncio
async def test_seed_urls_rewrites_apex_sitemap_to_www_root(monkeypatch):
    """A www root whose sitemap lists apex <loc>s must seed the rewritten www URLs,
    not drop them as off-host."""
    monkeypatch.setattr("app.auditor.discovery.guard_url", lambda url: True)

    sitemap = (
        '<urlset><url><loc>https://luranai.com/</loc></url>'
        '<url><loc>https://luranai.com/pricing</loc></url>'
        '<url><loc>https://luranai.com/faq</loc></url></urlset>'
    )

    async def fake_fetch(url):
        return sitemap if url.endswith("sitemap.xml") else None

    seeds = await discovery.seed_urls("https://www.luranai.com/", 10, fetch_text=fake_fetch)
    assert "https://www.luranai.com/pricing" in seeds
    assert "https://www.luranai.com/faq" in seeds
    # No apex-hosted seed survives.
    assert all("//luranai.com" not in s for s in seeds)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/auditor/test_canonicalize.py -v`
Expected: FAIL — `ImportError: cannot import name 'canonicalize_to_root'`.

- [ ] **Step 3: Add `canonicalize_to_root` to `discovery.py`**

Insert directly after `_same_registrable_host` (currently ~line 269-271):

```python
def canonicalize_to_root(url: str, root_host: str) -> str:
    """Rewrite url's host to root_host when they are the same registrable host
    (differ only by a leading 'www.'), but url's netloc is not already root_host.

    Split-host sites (apex on one CDN, www on another) list one host in their
    sitemap and link to the other; the crawl's exact same-host filter would drop
    those. Rewriting them to the canonical root host lets them be crawled while a
    genuinely different host (different subdomain/domain) is returned unchanged so
    the caller's filter still drops it. Scheme, path and query are preserved.
    """
    parsed = urlparse(url)
    if not parsed.netloc or parsed.netloc == root_host:
        return url
    root_hostname = urlparse(f"//{root_host}").hostname or root_host
    if _same_registrable_host(parsed.hostname or "", root_hostname):
        return urlunparse(parsed._replace(netloc=root_host))
    return url
```

(`urlparse`, `urlunparse` are already imported at the top of `discovery.py`.)

- [ ] **Step 4: Apply it in `seed_urls`**

In `seed_urls`, replace the sitemap loop body (currently):

```python
        for loc in _parse_sitemap(sitemap):
            n = normalize_url(loc)
            if n and _accept(n):
                seen.add(n)
                found.append(n)
```

with:

```python
        for loc in _parse_sitemap(sitemap):
            n = normalize_url(loc)
            if not n:
                continue
            n = canonicalize_to_root(n, root_host)
            if _accept(n):
                seen.add(n)
                found.append(n)
```

- [ ] **Step 5: Apply it in `crawl_site`**

In `crawler.py`, in the link-discovery loop of `crawl_site._run` (currently starts `for link in discovery._extract_links(page.html, url):`), insert the rewrite as the first line of the loop body, before the `if link in seen:` check:

```python
            for link in discovery._extract_links(page.html, url):
                link = discovery.canonicalize_to_root(link, root_host)
                if link in seen:
                    continue
                if urlparse(link).netloc != root_host:
                    seen.add(link)
                    continue
```

(The rest of the loop — structural prune, `guard_url`, append — is unchanged. `root_host` is already defined in `crawl_site`.)

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/auditor/test_canonicalize.py tests/auditor/test_discovery.py tests/auditor/test_crawler.py -v`
Expected: PASS (new file passes; existing discovery/crawler tests unaffected).

- [ ] **Step 7: Commit**

```bash
git add app/auditor/discovery.py app/auditor/crawler.py tests/auditor/test_canonicalize.py
git commit -m "feat(auditor): canonicalize split-host www/apex URLs into the root host"
```

---

### Task 2: Soft-block detection + bounded retry

**Files:**
- Modify: `app/core/config.py` (two new settings, in the auditor block ~line 223-234)
- Modify: `app/auditor/crawler.py` (`RawPageError.SOFT_BLOCKED`; refactor `render_page` into retry wrapper + `_render_once`)
- Test: `tests/auditor/test_crawler.py`

**Interfaces:**
- Produces: `crawler.RawPageError.SOFT_BLOCKED` (value `"soft_blocked"`) — set on a `RawPage` when the render returned HTTP 403 or 429. Consumed by Task 3.
- Consumes: `settings.AUDITOR_SOFT_BLOCK_RETRIES: int`, `settings.AUDITOR_SOFT_BLOCK_BACKOFF_S: list[int]`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/auditor/test_crawler.py`:

```python
def _fast_soft_block_settings():
    """Stub settings: retries=2, zero backoff (fast), generous timeout."""
    return type("S", (), {
        "AUDITOR_PER_PAGE_TIMEOUT_S": 30,
        "AUDITOR_HYDRATION_SETTLE_MS": 0,
        "AUDITOR_CRAWL_RPS": 1,
        "AUDITOR_SOFT_BLOCK_RETRIES": 2,
        "AUDITOR_SOFT_BLOCK_BACKOFF_S": [0, 0],
    })()


@pytest.mark.asyncio
async def test_render_page_retries_soft_block_then_succeeds(monkeypatch):
    monkeypatch.setattr("app.auditor.crawler.guard_url", lambda url: True)
    monkeypatch.setattr("app.auditor.crawler.settings", _fast_soft_block_settings())
    calls = {"n": 0}

    async def renderer(url):
        calls["n"] += 1
        if calls["n"] < 3:
            return 403, "<html>challenge</html>"
        return 200, "<html>real</html>"

    page = await crawler.render_page("https://x.com/a", page_renderer=renderer)
    assert calls["n"] == 3
    assert page.status == 200
    assert page.error_kind == crawler.RawPageError.OK


@pytest.mark.asyncio
async def test_render_page_soft_block_exhausts_retries(monkeypatch):
    monkeypatch.setattr("app.auditor.crawler.guard_url", lambda url: True)
    monkeypatch.setattr("app.auditor.crawler.settings", _fast_soft_block_settings())
    calls = {"n": 0}

    async def renderer(url):
        calls["n"] += 1
        return 403, "<html>challenge</html>"

    page = await crawler.render_page("https://x.com/a", page_renderer=renderer)
    assert calls["n"] == 3  # 1 initial + 2 retries
    assert page.status == 403
    assert page.error_kind == crawler.RawPageError.SOFT_BLOCKED


@pytest.mark.asyncio
async def test_render_page_429_treated_as_soft_block(monkeypatch):
    monkeypatch.setattr("app.auditor.crawler.guard_url", lambda url: True)
    monkeypatch.setattr("app.auditor.crawler.settings", _fast_soft_block_settings())
    calls = {"n": 0}

    async def renderer(url):
        calls["n"] += 1
        return (429, "<html>rl</html>") if calls["n"] == 1 else (200, "<html>ok</html>")

    page = await crawler.render_page("https://x.com/a", page_renderer=renderer)
    assert page.status == 200
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_render_page_200_not_retried(monkeypatch):
    monkeypatch.setattr("app.auditor.crawler.guard_url", lambda url: True)
    monkeypatch.setattr("app.auditor.crawler.settings", _fast_soft_block_settings())
    calls = {"n": 0}

    async def renderer(url):
        calls["n"] += 1
        return 200, "<html>ok</html>"

    page = await crawler.render_page("https://x.com/a", page_renderer=renderer)
    assert calls["n"] == 1
    assert page.error_kind == crawler.RawPageError.OK
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/auditor/test_crawler.py -k "soft_block or not_retried or 429" -v`
Expected: FAIL — `AttributeError: SOFT_BLOCKED` and/or wrong call counts (no retry yet).

- [ ] **Step 3: Add the settings**

In `app/core/config.py`, in the auditor block (right after `AUDITOR_PER_PAGE_TIMEOUT_S: int = 10`):

```python
    # Bot-firewall soft-block (HTTP 403/429) retries. Some sites (Vercel BotID,
    # Cloudflare) challenge a fraction of headless requests; a few retries on the
    # SAME browser session (so the clearance cookie carries) recovers most pages.
    AUDITOR_SOFT_BLOCK_RETRIES: int = 2
    AUDITOR_SOFT_BLOCK_BACKOFF_S: list[int] = [1, 3]
```

- [ ] **Step 4: Add the `SOFT_BLOCKED` enum member**

In `app/auditor/crawler.py`, in `class RawPageError`, add after `HTTP_ERROR`:

```python
    SOFT_BLOCKED = "soft_blocked"  # HTTP 403/429 — bot firewall / rate limit; retryable
```

- [ ] **Step 5: Refactor `render_page` into a retry wrapper + `_render_once`**

Rename the current body of `render_page` (everything after the pre-render `guard_url` block) into a new coroutine `_render_once(url, renderer, max_bytes)`, and change only the terminal `error_kind` line. Then make `render_page` the retry wrapper.

Replace the terminal classification in the (moved) body — currently:

```python
    error_kind = RawPageError.OK if 200 <= status < 400 else RawPageError.HTTP_ERROR
```

with:

```python
    if 200 <= status < 400:
        error_kind = RawPageError.OK
    elif status in (403, 429):
        error_kind = RawPageError.SOFT_BLOCKED
    else:
        error_kind = RawPageError.HTTP_ERROR
```

New structure:

```python
async def _render_once(url: str, renderer, max_bytes: int) -> RawPage:
    """One render attempt. Returns a RawPage (never raises). SOFT_BLOCKED for 403/429."""
    try:
        result = await asyncio.wait_for(
            renderer(url),
            timeout=settings.AUDITOR_PER_PAGE_TIMEOUT_S
            + settings.AUDITOR_HYDRATION_SETTLE_MS / 1000
            + 2,
        )
    except TimeoutError:
        return RawPage(url=url, status=0, html=None, bytes_len=0,
                       error="TIMEOUT", error_kind=RawPageError.TIMEOUT)
    except Exception as exc:
        logger.debug("render_page error {}: {}", url, exc)
        msg = str(exc).lower()
        kind = (
            RawPageError.LAUNCH_FAILED
            if ("executable doesn't exist" in msg or "browsertype.launch" in msg)
            else RawPageError.HTTP_ERROR
        )
        return RawPage(url=url, status=0, html=None, bytes_len=0, error=str(exc), error_kind=kind)

    if len(result) == 3:
        status, html, final_url = result
    else:
        status, html = result
        final_url = url

    if final_url != url and not guard_url(final_url):
        logger.warning("render_page SSRF post-redirect guard blocked: {} -> {}", url, final_url)
        return RawPage(url=url, status=0, html=None, bytes_len=0,
                       error="UNSAFE_REDIRECT", error_kind=RawPageError.BLOCKED, final_url=final_url)

    byte_len = len(html.encode("utf-8"))
    if byte_len > max_bytes:
        return RawPage(url=url, status=status, html=None, bytes_len=byte_len,
                       error="SIZE_CAP_EXCEEDED", error_kind=RawPageError.SIZE_CAP, final_url=final_url)

    if 200 <= status < 400:
        error_kind = RawPageError.OK
    elif status in (403, 429):
        error_kind = RawPageError.SOFT_BLOCKED
    else:
        error_kind = RawPageError.HTTP_ERROR
    return RawPage(url=url, status=status, html=html, bytes_len=byte_len,
                   error_kind=error_kind, final_url=final_url)


async def render_page(url: str, *, page_renderer=None, max_bytes: int = 2_000_000) -> RawPage:
    """Render a single page with SSRF guards and bounded soft-block retries.

    Pre-render SSRF guard, then up to AUDITOR_SOFT_BLOCK_RETRIES retries when the
    render returns a soft block (403/429), sleeping AUDITOR_SOFT_BLOCK_BACKOFF_S[i]
    between attempts and reusing the same injected renderer / browser session so a
    challenge-clearance cookie carries into the retry. Only 403/429 are retried.
    """
    if not guard_url(url):
        logger.warning("render_page SSRF pre-guard blocked: {}", url)
        return RawPage(url=url, status=0, html=None, bytes_len=0,
                       error="UNSAFE_URL", error_kind=RawPageError.BLOCKED)

    renderer = page_renderer or _playwright_render
    retries = settings.AUDITOR_SOFT_BLOCK_RETRIES
    backoff = settings.AUDITOR_SOFT_BLOCK_BACKOFF_S
    attempt = 0
    while True:
        page = await _render_once(url, renderer, max_bytes)
        if page.error_kind != RawPageError.SOFT_BLOCKED or attempt >= retries:
            return page
        if backoff:
            await asyncio.sleep(backoff[min(attempt, len(backoff) - 1)])
        attempt += 1
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/auditor/test_crawler.py -v`
Expected: PASS (new retry tests + all existing crawler tests, including the timeout/error/size-cap tests that `_render_once` still satisfies).

- [ ] **Step 7: Commit**

```bash
git add app/core/config.py app/auditor/crawler.py tests/auditor/test_crawler.py
git commit -m "feat(auditor): detect 403/429 soft-blocks and retry with backoff"
```

---

### Task 3: Honest `BLOCKED` scan-state

**Files:**
- Modify: `app/db/models/audit.py` (`ScanQuality.BLOCKED`, after `RENDER_FAILED` ~line 167)
- Create: `alembic/versions/<rev>_add_blocked_scan_quality.py`
- Modify: `app/auditor/scan_quality.py` (`_BLOCKED_REASON`; `BLOCKED` branch in `classify_scan_outcome`)
- Modify: `app/auditor/pipeline.py` (include `BLOCKED` in the fail-with-reason guard ~line 240)
- Test: `tests/auditor/test_scan_quality.py`

**Interfaces:**
- Consumes: `crawler.RawPageError.SOFT_BLOCKED` (Task 2).
- Produces: `ScanQuality.BLOCKED` (value `"blocked"`); `classify_scan_outcome` returns it when there is no content and the root page is `SOFT_BLOCKED`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/auditor/test_scan_quality.py`:

```python
def test_blocked_when_root_soft_blocked_and_no_content():
    q, reason = classify_scan_outcome(_root(RawPageError.SOFT_BLOCKED), _extracted(0),
                                      thin_min_content_chars=800)
    assert q == ScanQuality.BLOCKED
    assert "blocked our scanner" in reason.lower()


def test_soft_blocked_root_with_content_is_ok():
    """A soft-blocked homepage but readable subpages is a real result, not BLOCKED."""
    q, _ = classify_scan_outcome(_root(RawPageError.SOFT_BLOCKED), _extracted(1000),
                                 thin_min_content_chars=800)
    assert q == ScanQuality.OK


def test_soft_block_does_not_mask_timeout_unreachable():
    q, _ = classify_scan_outcome(_root(RawPageError.TIMEOUT), _extracted(0),
                                 thin_min_content_chars=800)
    assert q == ScanQuality.UNREACHABLE
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/auditor/test_scan_quality.py -k "blocked or soft" -v`
Expected: FAIL — `AttributeError: BLOCKED` on `ScanQuality`.

- [ ] **Step 3: Add the enum member**

In `app/db/models/audit.py`, in `class ScanQuality(str, enum.Enum)`, add after `RENDER_FAILED = "render_failed"`:

```python
    BLOCKED = "blocked"
```

- [ ] **Step 4: Create the Alembic migration**

Find the current head:

Run: `docker exec -i luran_api alembic heads` → expect `aa10enteredurl (head)`.

Create `alembic/versions/ab20_add_blocked_scan_quality.py`:

```python
"""add blocked to audit_scan_quality enum

Revision ID: ab20blockedquality
Revises: aa10enteredurl
Create Date: 2026-07-28
"""
from alembic import op

revision = "ab20blockedquality"
down_revision = "aa10enteredurl"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Additive enum value. Postgres 15 permits ADD VALUE inside a transaction as
    # long as the value is not used in the same transaction (it isn't here).
    op.execute("ALTER TYPE audit_scan_quality ADD VALUE IF NOT EXISTS 'blocked'")


def downgrade() -> None:
    # Postgres cannot drop an enum value without recreating the type; the extra
    # value is inert when unused, so downgrade is intentionally a no-op.
    pass
```

- [ ] **Step 5: Add the classification branch**

In `app/auditor/scan_quality.py`, add the reason constant near the other reasons:

```python
_BLOCKED_REASON = (
    "This site's bot/firewall protection blocked our scanner. Ask them to "
    "allowlist Luran, or import your content manually."
)
```

In `classify_scan_outcome`, insert the BLOCKED branch immediately after the `root_rejected` block and before the `root_page is None` block:

```python
    if root_rejected:
        return ScanQuality.UNREACHABLE, _UNREACHABLE_REASON
    if root_page is not None and root_page.error_kind == RawPageError.SOFT_BLOCKED:
        return ScanQuality.BLOCKED, _BLOCKED_REASON
    if root_page is None or root_page.error_kind == RawPageError.LAUNCH_FAILED:
        return ScanQuality.RENDER_FAILED, _RENDER_FAILED_REASON
```

(`RawPageError` is already imported at the top of `scan_quality.py`.)

- [ ] **Step 6: Make the pipeline fail with the honest reason**

In `app/auditor/pipeline.py`, extend the guard (currently `if quality in (ScanQuality.UNREACHABLE, ScanQuality.RENDER_FAILED):`) to:

```python
            if quality in (ScanQuality.UNREACHABLE, ScanQuality.RENDER_FAILED, ScanQuality.BLOCKED):
                raise ScanUnreadable(quality, reason)
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `pytest tests/auditor/test_scan_quality.py -v`
Expected: PASS.

- [ ] **Step 8: Apply the migration locally and verify**

Run: `docker exec -i luran_api alembic upgrade head`
Then: `docker exec -i luran_api alembic current` → expect `ab20blockedquality (head)`.
Verify the value exists:
`docker exec -i luran_db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT unnest(enum_range(NULL::audit_scan_quality));"` → includes `blocked`.
(If the psql user/db env vars differ, read them from the compose file; the point is to confirm `blocked` is a valid enum label.)

- [ ] **Step 9: Commit**

```bash
git add app/db/models/audit.py alembic/versions/ab20_add_blocked_scan_quality.py \
        app/auditor/scan_quality.py app/auditor/pipeline.py tests/auditor/test_scan_quality.py
git commit -m "feat(auditor): honest BLOCKED scan-state for bot-firewall refusals"
```

---

### Task 4: Realistic UA on a shared browser context

**Files:**
- Modify: `app/core/config.py` (`AUDITOR_USER_AGENT`, in the auditor block)
- Modify: `app/auditor/crawler.py` (`_render_page_in_browser` → context-based; `_browser_render_session` and `_playwright_render` create a shared UA context)
- Test: `tests/auditor/test_crawler.py`

**Interfaces:**
- Consumes: `settings.AUDITOR_USER_AGENT: str`.
- Produces: a single Playwright browser **context** per crawl carrying the UA, from which every page is opened (so challenge-clearance cookies persist across pages — this is what makes Task 2's retry effective in production).

- [ ] **Step 1: Write the failing test**

Append to `tests/auditor/test_crawler.py`:

```python
@pytest.mark.asyncio
async def test_playwright_render_uses_ua_context(monkeypatch):
    """_playwright_render must open pages from a context created with the
    configured user-agent, not browser.new_page() (which would use the default
    HeadlessChrome UA and an isolated per-page context)."""
    monkeypatch.setattr("app.auditor.crawler.resolve_and_validate_host",
                        lambda h: "93.184.216.34")

    captured = {}

    class FakePage:
        url = "https://x.com/"
        async def route(self, *a, **k): pass
        async def goto(self, *a, **k): return SimpleNamespace(status=200)
        async def wait_for_load_state(self, *a, **k): pass
        async def content(self): return "<html>ok</html>"
        async def close(self): pass

    class FakeContext:
        async def new_page(self): return FakePage()
        async def close(self): pass

    class FakeBrowser:
        async def new_context(self, **kwargs):
            captured.update(kwargs); return FakeContext()
        async def new_page(self):
            raise AssertionError("must not open pages directly on the browser")
        async def close(self): pass

    class FakeChromium:
        async def launch(self, **k): return FakeBrowser()

    class FakePW:
        chromium = FakeChromium()
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass

    monkeypatch.setattr("app.auditor.crawler.async_playwright", lambda: FakePW())

    status, html, final = await crawler._playwright_render("https://x.com/")
    assert status == 200
    assert captured.get("user_agent") == crawler.settings.AUDITOR_USER_AGENT
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/auditor/test_crawler.py::test_playwright_render_uses_ua_context -v`
Expected: FAIL — currently `browser.new_page()` is used, so `new_context` is never called (`captured` empty) and the `AssertionError` guard fires.

- [ ] **Step 3: Add the setting**

In `app/core/config.py` auditor block:

```python
    # Real desktop-Chrome UA for the crawl context — the default headless UA
    # ("HeadlessChrome") is an obvious bot tell to some firewalls.
    AUDITOR_USER_AGENT: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )
```

- [ ] **Step 4: Make renders open from a shared UA context**

In `app/auditor/crawler.py`:

Change `_render_page_in_browser(browser, url)` to take a context and open the page from it. Rename to `_render_page_in_context` and change the first line:

```python
async def _render_page_in_context(context, url: str) -> tuple[int, str, str]:
    """Render one URL in an already-created context (with UA) and return
    (status, html, final_url). Opens/closes a fresh page, applies the SSRF/asset
    route guard, waits for hydration best-effort, and snapshots."""
    page = await context.new_page()
    try:
        # ... body unchanged (page.route, page.goto, wait_for_load_state, content) ...
```

In `_browser_render_session`, create one UA context and render from it:

```python
    async with _CRAWL_SLOTS:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[f"--host-resolver-rules={resolver_rule}"],
            )
            try:
                context = await browser.new_context(user_agent=settings.AUDITOR_USER_AGENT)

                async def _render(url: str) -> tuple[int, str, str]:
                    return await _render_page_in_context(context, url)

                yield _render
            finally:
                await browser.close()
```

`_playwright_render` already **delegates** to `_browser_render_session(host, safe_ip)` (it resolves+pins the host, then `async with _browser_render_session(...) as render: return await render(url)`). Leave `_playwright_render` **unchanged** — routing every render through `_browser_render_session` means the UA context added above is the single source of the UA + shared cookies for both the standalone path (the test) and the full `crawl_site` path.

> Invariant to satisfy: exactly one `browser.new_context(user_agent=settings.AUDITOR_USER_AGENT)` per crawl, every page opened from that context, and no remaining `browser.new_page()` call. Only two functions change: `_render_page_in_browser` → `_render_page_in_context`, and `_browser_render_session`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/auditor/test_crawler.py -v`
Expected: PASS (new UA test + all existing crawler tests).

- [ ] **Step 6: Commit**

```bash
git add app/core/config.py app/auditor/crawler.py tests/auditor/test_crawler.py
git commit -m "feat(auditor): render from a shared context with a realistic user-agent"
```

---

### Task 5: Full-suite regression + SOP

**Files:**
- Create/Modify: `docs/sop/auditor_bot_protection_resilience.md`

- [ ] **Step 1: Run the full auditor suite**

Run: `pytest tests/auditor/ -v`
Expected: PASS, except the two pre-existing network-dependent hangs noted in the repo (`test_chat_grounding.py`, `test_service_import.py` — tiktoken first-use download; excluded locally, run in CI). Ruff clean: `python -m ruff check app/auditor app/core/config.py app/db/models/audit.py`.

- [ ] **Step 2: Write the SOP**

Create `docs/sop/auditor_bot_protection_resilience.md` covering: what shipped (the four units), why (the 2026-07-28 Vercel-403 + split-host investigation), how (canonicalize / retry / BLOCKED state / UA context), files touched, verification (tests + the worker repro that found it), deploy note (enum migration must precede code use; CD runs `alembic upgrade`), and follow-ups (challenge-solving, proxy egress, FE "import manually" CTA, worker-IP Vercel allowlist).

- [ ] **Step 3: Commit**

```bash
git add docs/sop/auditor_bot_protection_resilience.md
git commit -m "docs(sop): auditor bot-protection resilience"
```

---

## Self-Review

**Spec coverage:**
- Unit 1 (canonicalization) → Task 1. ✓
- Unit 2 (retry + SOFT_BLOCKED) → Task 2. ✓
- Unit 3 (BLOCKED state + migration + pipeline) → Task 3. ✓
- Unit 4 (realistic UA + shared context) → Task 4. ✓
- New settings (`AUDITOR_SOFT_BLOCK_RETRIES`, `AUDITOR_SOFT_BLOCK_BACKOFF_S`, `AUDITOR_USER_AGENT`) → Tasks 2 & 4. ✓
- Testing matrix from the spec → covered across Tasks 1-3 tests; UA seam → Task 4 test. ✓
- Deploy/rollback note → Task 3 Step 8 + Task 5 SOP. ✓
- Non-goals (challenge-solving, proxy, FE CTA) → excluded; recorded as follow-ups. ✓

**Placeholder scan:** No TBD/TODO; every code step shows the code. Task 4 Step 4 carries one conditional instruction (delegation vs inline launch) with an explicit invariant to satisfy — resolved, not a placeholder.

**Type consistency:** `canonicalize_to_root(url, root_host) -> str` used identically in Tasks 1 (both call sites). `RawPageError.SOFT_BLOCKED` produced in Task 2, consumed in Task 3. `ScanQuality.BLOCKED` produced in Task 3 model, migration, classifier, pipeline — names match. `settings.AUDITOR_SOFT_BLOCK_RETRIES/BACKOFF_S/USER_AGENT` names consistent across config + crawler.
