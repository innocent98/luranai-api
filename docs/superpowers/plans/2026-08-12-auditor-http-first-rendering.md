# Auditor HTTP-first Rendering Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Render auditor pages via a self-hosted HTTP-first path (`curl_cffi` with Chrome TLS fingerprint) that bypasses Vercel/Cloudflare bot-blocking of the VPS datacenter IP, falling back to the existing local headless render only for genuinely client-rendered pages.

**Architecture:** A new per-URL renderer `_http_first_render` fetches raw HTML over `curl_cffi` (SSRF-pinned) and, only when the extracted text is below a threshold, falls back to `_playwright_render`. A backend selector (`AUDITOR_RENDER_BACKEND`, default `http`) chooses it inside the existing `page_renderer` seam, so `pipeline.py`/`service.py` are untouched. Default `http` in dev and prod removes the dev/prod divergence that caused the original failure.

**Tech Stack:** Python 3.11, FastAPI, Poetry, pytest + pytest-asyncio, `curl_cffi` (new), Playwright (existing, now fallback-only), trafilatura/BeautifulSoup (existing extraction).

## Global Constraints

- Change lives ONLY in `app/auditor/crawler.py` + `app/core/config.py` (+ `pyproject.toml`/`poetry.lock` for the dep). `pipeline.py` and `service.py` MUST stay untouched.
- SSRF parity is mandatory on the HTTP path: every fetch resolves the host via `resolve_and_validate_host`, pins the connection to that IP, and re-guards every redirect hop; fail-closed (`return 0, "", url`) on `ValueError`. Egress is from OUR network, so this is load-bearing.
- The `local` headless backend (`_playwright_render` / `_browser_render_session`) is KEPT as dev/ops fallback — never deleted.
- Renderer contract is unchanged: `(url) -> tuple[int, str, str]` == `(status, html, final_url)`.
- Follow existing test conventions in `tests/auditor/test_crawler.py`: `pytest.mark.asyncio`, `unittest.mock.AsyncMock`/`patch`, `types.SimpleNamespace`, patch targets on `app.auditor.crawler.*`.
- No commit uses the Claude co-author trailer (repo rule).

---

### Task 1: Add `curl_cffi` dependency + auditor HTTP config

**Files:**
- Modify: `pyproject.toml` (`[tool.poetry.dependencies]`)
- Modify: `poetry.lock` (regenerated)
- Modify: `app/core/config.py` (add 4 settings + `Literal` import)
- Test: `tests/auditor/test_crawler.py`

**Interfaces:**
- Produces: `settings.AUDITOR_RENDER_BACKEND: Literal["http","local"]` (default `"http"`), `settings.AUDITOR_HTTP_MIN_CONTENT_CHARS: int` (500), `settings.AUDITOR_HTTP_IMPERSONATE: str` (`"chrome"`), `settings.AUDITOR_HTTP_TIMEOUT_S: int` (15). `curl_cffi.requests.AsyncSession` importable.

- [ ] **Step 1: Write the failing test**

Add to `tests/auditor/test_crawler.py`:

```python
def test_auditor_http_backend_config_defaults():
    from app.core.config import settings

    assert settings.AUDITOR_RENDER_BACKEND == "http"
    assert settings.AUDITOR_HTTP_MIN_CONTENT_CHARS == 500
    assert settings.AUDITOR_HTTP_IMPERSONATE == "chrome"
    assert settings.AUDITOR_HTTP_TIMEOUT_S == 15


def test_curl_cffi_importable():
    from curl_cffi.requests import AsyncSession  # noqa: F401
```

- [ ] **Step 2: Run test to verify it fails**

Run: `poetry run pytest tests/auditor/test_crawler.py::test_auditor_http_backend_config_defaults tests/auditor/test_crawler.py::test_curl_cffi_importable -v`
Expected: FAIL — `AttributeError: ... AUDITOR_RENDER_BACKEND` and `ModuleNotFoundError: curl_cffi`.

- [ ] **Step 3: Add the dependency**

Run: `poetry add curl_cffi`
(Let Poetry resolve the latest release — do not hand-pin a stale minor. This installs the manylinux wheel — `curl_cffi` bundles its own `libcurl-impersonate`, so NO extra apt packages and NO Dockerfile change are needed; the worker image already runs `poetry install --only main` from `pyproject.toml`+`poetry.lock`.)

Note: `AUDITOR_HTTP_IMPERSONATE="chrome"` is a rolling alias in recent `curl_cffi`. If the installed version rejects `"chrome"`, set it to a concrete target the version supports (e.g. `"chrome124"`) — it's config, no code change.

- [ ] **Step 4: Add the config settings**

In `app/core/config.py`, add `Literal` to the typing import at the top:

```python
from typing import Literal
```

Then add these fields inside the `Settings` class, next to the other `AUDITOR_*` fields (after `AUDITOR_USER_AGENT`, around line 249):

```python
    # Render backend: "http" = self-hosted HTTP-first fetch (curl_cffi, Chrome TLS)
    # with per-page headless fallback (default, dev + prod — same path everywhere).
    # "local" = headless-only (debugging / explicit override / ops fallback).
    AUDITOR_RENDER_BACKEND: Literal["http", "local"] = "http"
    # Below this many extracted chars, the HTTP fetch is treated as a thin/SPA
    # shell and the page falls back to a headless render.
    AUDITOR_HTTP_MIN_CONTENT_CHARS: int = 500
    # curl_cffi browser-impersonation target (TLS/JA3 + browser-consistent headers).
    AUDITOR_HTTP_IMPERSONATE: str = "chrome"
    AUDITOR_HTTP_TIMEOUT_S: int = 15
```

- [ ] **Step 5: Run test to verify it passes**

Run: `poetry run pytest tests/auditor/test_crawler.py::test_auditor_http_backend_config_defaults tests/auditor/test_crawler.py::test_curl_cffi_importable -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml poetry.lock app/core/config.py tests/auditor/test_crawler.py
git commit -m "feat(auditor): add curl_cffi dep + HTTP-first render config"
```

---

### Task 2: `_curl_cffi_fetch` — SSRF-pinned HTTP fetch with redirect re-guard

**Files:**
- Modify: `app/auditor/crawler.py` (add module-top import + new function)
- Test: `tests/auditor/test_crawler.py`

**Interfaces:**
- Consumes: `settings.AUDITOR_HTTP_IMPERSONATE`, `settings.AUDITOR_HTTP_TIMEOUT_S`, `resolve_and_validate_host` (already imported in crawler.py).
- Produces: `async def _curl_cffi_fetch(url: str, *, max_hops: int = 3) -> tuple[int, str, str]` returning `(status, html, final_url)`; `(0, "", url)` on SSRF failure/invalid scheme/too many redirects. Module-level `AsyncSession` (imported) so tests patch `app.auditor.crawler.AsyncSession`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/auditor/test_crawler.py`:

```python
def _fake_async_session(responses):
    """Build a mock usable as `async with AsyncSession() as s`.
    `responses` is a list of SimpleNamespace(status_code=, text=, headers={}) returned
    by successive s.get(...) calls."""
    session = AsyncMock()
    session.get = AsyncMock(side_effect=list(responses))
    ctx = AsyncMock()
    ctx.__aenter__.return_value = session
    ctx.__aexit__.return_value = False
    factory = lambda *a, **k: ctx  # noqa: E731
    return factory, session


@pytest.mark.asyncio
async def test_curl_cffi_fetch_pins_ip_and_returns_tuple():
    resp = SimpleNamespace(status_code=200, text="<html>ok</html>", headers={})
    factory, session = _fake_async_session([resp])
    with patch("app.auditor.crawler.AsyncSession", factory), patch(
        "app.auditor.crawler.resolve_and_validate_host", return_value="93.184.216.34"
    ):
        status, html, final_url = await crawler._curl_cffi_fetch("https://example.com/a")
    assert (status, html, final_url) == (200, "<html>ok</html>", "https://example.com/a")
    # Connection pinned to the validated IP for host:port
    _, kwargs = session.get.call_args
    assert kwargs["resolve"] == ["example.com:443:93.184.216.34"]
    assert kwargs["allow_redirects"] is False
    assert kwargs["impersonate"] == "chrome"


@pytest.mark.asyncio
async def test_curl_cffi_fetch_blocks_unsafe_host():
    with patch(
        "app.auditor.crawler.resolve_and_validate_host", side_effect=ValueError("private")
    ):
        result = await crawler._curl_cffi_fetch("http://10.0.0.1/x")
    assert result == (0, "", "http://10.0.0.1/x")


@pytest.mark.asyncio
async def test_curl_cffi_fetch_reguards_each_redirect_hop():
    # hop 1 redirects to an internal host -> must be blocked on re-guard, no body
    r1 = SimpleNamespace(status_code=307, text="", headers={"location": "http://169.254.169.254/"})
    factory, session = _fake_async_session([r1])

    def guard(host):
        if host == "example.com":
            return "93.184.216.34"
        raise ValueError("internal")

    with patch("app.auditor.crawler.AsyncSession", factory), patch(
        "app.auditor.crawler.resolve_and_validate_host", side_effect=guard
    ):
        status, html, final_url = await crawler._curl_cffi_fetch("https://example.com/a")
    assert (status, html) == (0, "")
    assert final_url == "http://169.254.169.254/"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `poetry run pytest tests/auditor/test_crawler.py -k curl_cffi_fetch -v`
Expected: FAIL — `AttributeError: module 'app.auditor.crawler' has no attribute '_curl_cffi_fetch'` (and no `AsyncSession`).

- [ ] **Step 3: Add the module-top import**

In `app/auditor/crawler.py`, near the other third-party imports (after `import httpx`, around line 12):

```python
from curl_cffi.requests import AsyncSession
```

- [ ] **Step 4: Implement `_curl_cffi_fetch`**

Add this function to `app/auditor/crawler.py` (place it just above `_playwright_render`, ~line 302). `curl_cffi`'s `resolve=` makes libcurl connect to the pinned IP while keeping the URL's hostname for the `Host` header and TLS SNI automatically, and `impersonate` sets a browser-consistent header set — so no manual `Host`/`Accept` juggling is needed.

```python
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}


async def _curl_cffi_fetch(url: str, *, max_hops: int = 3) -> tuple[int, str, str]:
    """Fetch a URL over curl_cffi (Chrome TLS/JA3) with SSRF pinning.

    Resolves + validates the host, pins the connection to that IP (curl --resolve),
    and re-guards every redirect hop (follow_redirects is disabled and handled
    manually so a Location cannot bounce us to an internal host). Returns
    (status, html, final_url); (0, "", url) on any SSRF failure, bad scheme, or
    exceeding max_hops — treated downstream as blocked/error.
    """
    current = url
    async with AsyncSession() as session:
        for _ in range(max_hops + 1):
            parsed = urlparse(current)
            host = parsed.hostname or ""
            if parsed.scheme not in {"http", "https"} or not host:
                return 0, "", current
            try:
                safe_ip = resolve_and_validate_host(host)
            except ValueError:
                logger.warning("_curl_cffi_fetch SSRF guard blocked: {}", current)
                return 0, "", current
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            # Pin only when the guard returned an IP (not an allowlisted hostname).
            resolve_rule = [f"{host}:{port}:{safe_ip}"] if safe_ip != host else None
            try:
                resp = await session.get(
                    current,
                    impersonate=settings.AUDITOR_HTTP_IMPERSONATE,
                    resolve=resolve_rule,
                    allow_redirects=False,
                    timeout=settings.AUDITOR_HTTP_TIMEOUT_S,
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("_curl_cffi_fetch error {}: {}", current, exc)
                return 0, "", current
            if resp.status_code in _REDIRECT_STATUSES:
                location = resp.headers.get("location")
                if not location:
                    return resp.status_code, resp.text or "", current
                current = urljoin(current, location)
                continue
            return resp.status_code, resp.text or "", current
    return 0, "", current
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `poetry run pytest tests/auditor/test_crawler.py -k curl_cffi_fetch -v`
Expected: PASS (3 tests).

- [ ] **Step 6: Commit**

```bash
git add app/auditor/crawler.py tests/auditor/test_crawler.py
git commit -m "feat(auditor): SSRF-pinned curl_cffi fetch with per-hop redirect re-guard"
```

---

### Task 3: `_http_first_render` — HTTP-first with headless fallback

**Files:**
- Modify: `app/auditor/crawler.py` (new function)
- Test: `tests/auditor/test_crawler.py`

**Interfaces:**
- Consumes: `_curl_cffi_fetch`, `_playwright_render`, `settings.AUDITOR_HTTP_MIN_CONTENT_CHARS`, `extract_main` (from `app.auditor.extraction`, imported lazily to avoid the crawler↔extraction import cycle).
- Produces: `async def _http_first_render(url: str) -> tuple[int, str, str]`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/auditor/test_crawler.py`:

```python
@pytest.mark.asyncio
async def test_http_first_render_returns_ssr_without_headless_fallback():
    big_html = "<html><body>" + ("word " * 400) + "</body></html>"  # > 500 chars of text
    with patch(
        "app.auditor.crawler._curl_cffi_fetch",
        AsyncMock(return_value=(200, big_html, "https://example.com/a")),
    ), patch("app.auditor.crawler._playwright_render", AsyncMock()) as fallback:
        status, html, final_url = await crawler._http_first_render("https://example.com/a")
    assert (status, final_url) == (200, "https://example.com/a")
    assert html == big_html
    fallback.assert_not_awaited()


@pytest.mark.asyncio
async def test_http_first_render_falls_back_when_thin():
    shell = "<html><body><div id='__next'></div></body></html>"  # ~0 text
    with patch(
        "app.auditor.crawler._curl_cffi_fetch",
        AsyncMock(return_value=(200, shell, "https://example.com/a")),
    ), patch(
        "app.auditor.crawler._playwright_render",
        AsyncMock(return_value=(200, "<html>rendered content</html>", "https://example.com/a")),
    ) as fallback:
        status, html, _ = await crawler._http_first_render("https://example.com/a")
    fallback.assert_awaited_once_with("https://example.com/a")
    assert html == "<html>rendered content</html>"


@pytest.mark.asyncio
async def test_http_first_render_falls_back_on_non_2xx():
    with patch(
        "app.auditor.crawler._curl_cffi_fetch",
        AsyncMock(return_value=(403, "", "https://example.com/a")),
    ), patch(
        "app.auditor.crawler._playwright_render",
        AsyncMock(return_value=(403, "", "https://example.com/a")),
    ) as fallback:
        await crawler._http_first_render("https://example.com/a")
    fallback.assert_awaited_once_with("https://example.com/a")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `poetry run pytest tests/auditor/test_crawler.py -k http_first_render -v`
Expected: FAIL — `_http_first_render` not defined.

- [ ] **Step 3: Implement `_http_first_render`**

Add to `app/auditor/crawler.py`, just below `_curl_cffi_fetch`:

```python
async def _http_first_render(url: str) -> tuple[int, str, str]:
    """HTTP-first renderer: fetch raw HTML over curl_cffi and fall back to a
    headless render only when the HTTP HTML is a thin/SPA shell.

    SSR/SSG sites (the common case, and the ones bot-blocked on the headless
    path) resolve here with no browser launched. A genuine client-rendered page
    yields too little text and falls back to _playwright_render for that one URL.
    """
    # Local import: extraction imports crawler (RawPage), so importing it at
    # module top would be a cycle.
    from app.auditor.extraction import extract_main

    status, html, final_url = await _curl_cffi_fetch(url)
    if 200 <= status < 400 and html:
        _, text = extract_main(html)
        if len(text) >= settings.AUDITOR_HTTP_MIN_CONTENT_CHARS:
            return status, html, final_url
    # Thin, empty, or non-2xx/3xx → headless fallback for this URL.
    return await _playwright_render(url)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `poetry run pytest tests/auditor/test_crawler.py -k http_first_render -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add app/auditor/crawler.py tests/auditor/test_crawler.py
git commit -m "feat(auditor): HTTP-first renderer with per-page headless fallback"
```

---

### Task 4: Backend selection wiring into `render_page` + `crawl_site`

**Files:**
- Modify: `app/auditor/crawler.py` (add `_select_backend`, `_default_renderer`; edit `render_page:448` and the `crawl_site` tail `~627`)
- Test: `tests/auditor/test_crawler.py`

**Interfaces:**
- Consumes: `settings.AUDITOR_RENDER_BACKEND`, `_http_first_render`, `_playwright_render`.
- Produces: `def _select_backend() -> str` (`"http"` unless backend set to `"local"`); `def _default_renderer()` returning the per-URL renderer callable.

- [ ] **Step 1: Write the failing tests**

Add to `tests/auditor/test_crawler.py`:

```python
def test_select_backend_defaults_to_http_local_when_overridden():
    with patch.object(crawler.settings, "AUDITOR_RENDER_BACKEND", "http"):
        assert crawler._select_backend() == "http"
        assert crawler._default_renderer() is crawler._http_first_render
    with patch.object(crawler.settings, "AUDITOR_RENDER_BACKEND", "local"):
        assert crawler._select_backend() == "local"
        assert crawler._default_renderer() is crawler._playwright_render


@pytest.mark.asyncio
async def test_crawl_site_http_backend_uses_http_renderer_no_shared_browser():
    seeds = ["https://example.com/"]
    with patch.object(crawler.settings, "AUDITOR_RENDER_BACKEND", "http"), patch(
        "app.auditor.crawler.resolve_and_validate_host", return_value="93.184.216.34"
    ), patch(
        "app.auditor.crawler._browser_render_session"
    ) as shared_browser, patch(
        "app.auditor.crawler._http_first_render",
        AsyncMock(return_value=(200, "<html><body>" + ("x " * 400) + "</body></html>",
                                "https://example.com/")),
    ) as http_render:
        pages = await crawler.crawl_site(seeds, max_pages=1)
    shared_browser.assert_not_called()  # HTTP backend launches no shared browser
    http_render.assert_awaited()
    assert len(pages) == 1 and pages[0].status == 200


@pytest.mark.asyncio
async def test_crawl_site_injected_renderer_still_overrides_backend():
    seeds = ["https://example.com/"]
    injected = AsyncMock(return_value=(200, "<html>content here</html>", "https://example.com/"))
    with patch.object(crawler.settings, "AUDITOR_RENDER_BACKEND", "http"), patch(
        "app.auditor.crawler._http_first_render", AsyncMock()
    ) as http_render:
        pages = await crawler.crawl_site(seeds, max_pages=1, page_renderer=injected)
    injected.assert_awaited()
    http_render.assert_not_awaited()
    assert len(pages) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `poetry run pytest tests/auditor/test_crawler.py -k "select_backend or crawl_site_http_backend or injected_renderer_still" -v`
Expected: FAIL — `_select_backend` / `_default_renderer` not defined; `crawl_site` still launches the shared browser.

- [ ] **Step 3: Add the selector helpers**

Add to `app/auditor/crawler.py` (just above `render_page`, ~line 405):

```python
def _select_backend() -> str:
    """'local' only when explicitly set; 'http' otherwise (default)."""
    return "local" if settings.AUDITOR_RENDER_BACKEND == "local" else "http"


def _default_renderer():
    """Per-URL renderer for the active backend (used when no renderer is injected)."""
    return _http_first_render if _select_backend() == "http" else _playwright_render
```

- [ ] **Step 4: Wire `render_page` to the default renderer**

In `app/auditor/crawler.py`, change line 448:

```python
    renderer = page_renderer or _playwright_render
```

to:

```python
    renderer = page_renderer or _default_renderer()
```

- [ ] **Step 5: Wire the `crawl_site` tail to the HTTP backend**

In `app/auditor/crawler.py`, replace the tail (currently lines ~626-638):

```python
    # Injected renderer (tests) or empty frontier: no shared browser needed.
    if page_renderer is not None or not root_hostname:
        return await _run(page_renderer)

    # Production: launch ONE browser for the whole same-host crawl and reuse it
    # across every page (avoids a browser launch per page).
    try:
        safe_ip = resolve_and_validate_host(root_hostname)
    except ValueError:
        logger.warning("crawl_site SSRF guard blocked root host: {}", root_hostname)
        return []
    async with _browser_render_session(root_hostname, safe_ip) as renderer:
        return await _run(renderer)
```

with:

```python
    # Injected renderer (tests) or empty frontier: no shared browser needed.
    if page_renderer is not None or not root_hostname:
        return await _run(page_renderer)

    # HTTP backend (default): each page is fetched over HTTP; only thin/SPA pages
    # self-launch a headless render. No shared browser for the crawl.
    if _select_backend() == "http":
        return await _run(_http_first_render)

    # Local backend: launch ONE browser for the whole same-host crawl and reuse
    # it across every page (avoids a browser launch per page).
    try:
        safe_ip = resolve_and_validate_host(root_hostname)
    except ValueError:
        logger.warning("crawl_site SSRF guard blocked root host: {}", root_hostname)
        return []
    async with _browser_render_session(root_hostname, safe_ip) as renderer:
        return await _run(renderer)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `poetry run pytest tests/auditor/test_crawler.py -k "select_backend or crawl_site_http_backend or injected_renderer_still" -v`
Expected: PASS (3 tests).

- [ ] **Step 7: Commit**

```bash
git add app/auditor/crawler.py tests/auditor/test_crawler.py
git commit -m "feat(auditor): select HTTP-first render backend by default in crawl_site/render_page"
```

---

### Task 5: Full-suite verification, lint, and SOP

**Files:**
- Create: `docs/sop/auditor_http_first_rendering.md`
- (verification only) `tests/auditor/`, `app/auditor/crawler.py`, `app/core/config.py`

**Interfaces:** none (integration/verification task).

- [ ] **Step 1: Run the full auditor + crawler suite**

Run: `poetry run pytest tests/auditor/ -v`
Expected: PASS — all pre-existing tests (the local `_browser_render_session` path is untouched for `AUDITOR_RENDER_BACKEND=local`, injected-renderer tests still pass) plus the new tests. If any pre-existing test defaulted to the headless path via `crawl_site` with no injected renderer, it will now hit `_http_first_render`; fix by either injecting a renderer (preferred) or patching `settings.AUDITOR_RENDER_BACKEND="local"` in that test. Record which, if any.

- [ ] **Step 2: Lint / format**

Run: `poetry run ruff check app/auditor/crawler.py app/core/config.py && poetry run black --check app/auditor/crawler.py app/core/config.py`
Expected: clean (fix and re-run if not).

- [ ] **Step 3: Write the SOP**

Create `docs/sop/auditor_http_first_rendering.md` covering: what shipped (HTTP-first render backend), why (Vercel/datacenter-IP headless 403; measured linchpin — worker plain-HTTP 200 with full SSR bodies), how (`curl_cffi` SSRF-pinned fetch + thinness-gated headless fallback + `_select_backend`), files touched (`crawler.py`, `config.py`, `pyproject.toml`), config/env (`AUDITOR_RENDER_BACKEND=http` default; `local` to roll back), verification (unit tests + staging live smoke), rollback (set `AUDITOR_RENDER_BACKEND=local`), and follow-ups (relax worker memory/concurrency; add render-path metrics; ZenRows managed backend if the SPA-behind-WAF tail proves material). Match the existing `docs/sop/` style.

- [ ] **Step 4: Commit**

```bash
git add docs/sop/auditor_http_first_rendering.md
git commit -m "docs(sop): auditor HTTP-first rendering"
```

- [ ] **Step 5: Live staging smoke (manual, after deploy — not a code step)**

With `AUDITOR_RENDER_BACKEND=http` on staging, run an audit for `beyrictech.com` and `luranai.com`. Expected: report COMPLETED with real content/score, and logs show no headless launch for the SSR pages. If a scan still `BLOCKED`s, capture whether curl_cffi returned non-200 (that would mean plain HTTP is now challenged too — escalate to the ZenRows follow-up).

---

## Notes for the implementer

- The renderer contract `(status, html, final_url)` is consumed by `_render_once` (already handles 3-tuples) → `RawPage`. 403/429 map to `SOFT_BLOCKED`, which `render_page` retries; the HTTP path already fell back to headless before returning a 403, so a returned 403 means both paths failed (honest `BLOCKED`).
- Do NOT touch `pipeline.py` or `service.py`. They call `crawl_site(..., page_renderer=None)`; backend selection happens entirely inside `crawl_site`.
- `curl_cffi` `resolve=` keeps the URL hostname for `Host`/SNI automatically; do not add a manual `Host` header (it would fight the impersonation fingerprint).
```
