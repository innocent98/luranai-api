# Auditor Redirect Canonicalization — Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the auditor follow host-canonicalization redirects (apex→www, scheme upgrade) silently, require explicit confirmation before scanning a genuinely different host, and record the URL actually scanned — fixing audits of redirecting sites without weakening SSRF protection.

**Architecture:** All auditor submits funnel through `AuditService.start_audit`. Add a bounded, per-hop-re-guarded redirect resolver in `discovery.py`; classify the entered→final host change; canonicalize the report's URL for same-site redirects; raise a typed exception for different-host redirects that both endpoints map to HTTP 409. A nullable `entered_url` column preserves what the user typed. Frontend confirm modals are a separate plan.

**Tech Stack:** Python 3.11, FastAPI, SQLAlchemy, Alembic, httpx, pytest. Design spec: `docs/superpowers/specs/2026-07-25-auditor-redirect-canonicalization-design.md`.

## Global Constraints

- **SSRF: re-guard every redirect hop from scratch.** Each hop calls `resolve_and_validate_host(host)` and pins the connection to the validated IP; never reuse a prior pinned IP; never trust a `Location` host without re-validating. The crawl fetch (`crawler._render_page_in_browser`) and `discovery._default_fetch_text` keep their existing behavior — the resolver is the ONLY new place that follows redirects.
- **`max_hops = 3`.** Loop or exceeding the cap → `ValueError` (blocked).
- **Tier 1 (`www`/scheme toggle) is silent.** Only a genuinely different host triggers confirmation.
- **Never trust the client's `confirmed_target`.** The server re-resolves and compares; mismatch re-prompts, never scans.
- **Confirmation/blocked responses must NOT spend a public scan credit** (the 3/day limit); only an enqueued audit does. A generous per-IP anti-abuse limiter covers all attempts.
- **Block/error messages stay non-revealing** — never echo a resolved internal IP.
- **API stays on `/api/v1`; changes are additive** (optional request field, nullable column, new 409 code).
- **Commits must NOT include a Claude co-author trailer.**

## Running the test suite

Local machine has no venv/pytest; tests run in the `luran-api:latest` image against a disposable Postgres. Start it once:

```bash
docker run -d --rm --name luran_citest -e POSTGRES_USER=test_user -e POSTGRES_PASSWORD=test_password -e POSTGRES_DB=test_db -p 5544:5432 postgres:15-alpine
```

Run any test file/selector with:

```bash
docker run --rm -v "$PWD":/app -w /app \
  -e DATABASE_URL="postgresql://test_user:test_password@host.docker.internal:5544/test_db" \
  luran-api:latest sh -c "pip install -q pytest==8.3.4 pytest-asyncio==0.24.0 pytest-mock==3.14.0 pytest-cov==6.0.0 >/dev/null 2>&1 && python -m pytest <SELECTOR> -p no:cacheprovider -o addopts='' -q"
```

Ruff (CI lints `app/` only): `docker run --rm -v "$PWD":/app -w /app luran-api:latest sh -c "pip install -q ruff==0.8.6 >/dev/null 2>&1 && ruff check app/"`.

Tear down when done: `docker stop luran_citest`.

---

## File Structure

- `alembic/versions/<new>_add_entered_url_to_audit_reports.py` — new migration (chains from head `b7d21c9f3a04`).
- `app/db/models/audit.py` — add `entered_url` column to `AuditReport`.
- `app/auditor/discovery.py` — add `ResolvedRoot`, `resolve_redirect_chain`, `_guarded_get`, `classify_redirect`.
- `app/auditor/service.py` — add `RedirectConfirmationRequired`; canonicalize + confirm gating in `start_audit`.
- `app/schemas/auditor.py` — add optional `confirmed_target` to `PublicAuditRequest` and `DashboardAuditRequest`.
- `app/api/v1/endpoints/public_auditor.py` — 409 mapping, scan-credit-on-enqueue, generous anti-abuse limiter, expose `entered_url`/`pages_scanned`.
- `app/api/v1/endpoints/auditor.py` — 409 mapping, pass `confirmed_target`.
- Tests: `tests/auditor/test_redirect_resolver.py`, `tests/auditor/test_redirect_classifier.py`, `tests/auditor/test_start_audit_redirect.py`, `tests/auditor/test_public_auditor_redirect.py`.

---

## Task 1: `entered_url` column + migration

**Files:**
- Modify: `app/db/models/audit.py:206` (add column after `url`)
- Create: `alembic/versions/aa10_add_entered_url_to_audit_reports.py`
- Test: `tests/auditor/test_start_audit_redirect.py` (created here, extended in Task 4)

**Interfaces:**
- Produces: `AuditReport.entered_url: Mapped[str | None]` — the URL the user typed when it differs from the canonical `url`; NULL when they are equal.

- [ ] **Step 1: Add the column to the model**

In `app/db/models/audit.py`, immediately after the `url` column (line 206):

```python
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    # What the user typed, when it differs from the canonical `url` we scanned
    # (e.g. entered luranai.com, scanned www.luranai.com after an apex→www
    # redirect). NULL means entered == scanned. Display-only; never seeded from.
    entered_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
```

- [ ] **Step 2: Confirm the current migration head**

Run (needs the citest DB up):
```bash
docker run --rm -v "$PWD":/app -w /app -e DATABASE_URL="postgresql://test_user:test_password@host.docker.internal:5544/test_db" luran-api:latest sh -c "pip install -q alembic >/dev/null 2>&1 && alembic heads"
```
Expected: a single head `b7d21c9f3a04`. If more than one head prints, STOP and escalate (pre-existing divergence must be merged first — not part of this task).

- [ ] **Step 3: Create the migration**

Create `alembic/versions/aa10_add_entered_url_to_audit_reports.py`:

```python
"""add entered_url to audit_reports

Revision ID: aa10enteredurl
Revises: b7d21c9f3a04
Create Date: 2026-07-25

"""
from alembic import op
import sqlalchemy as sa

revision = "aa10enteredurl"
down_revision = "b7d21c9f3a04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "audit_reports",
        sa.Column("entered_url", sa.String(length=2048), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("audit_reports", "entered_url")
```

- [ ] **Step 4: Apply the migration and verify it round-trips**

```bash
docker run --rm -v "$PWD":/app -w /app -e DATABASE_URL="postgresql://test_user:test_password@host.docker.internal:5544/test_db" luran-api:latest sh -c "pip install -q alembic >/dev/null 2>&1 && alembic upgrade head && alembic downgrade -1 && alembic upgrade head && echo MIGRATION_OK"
```
Expected: ends with `MIGRATION_OK`, no errors.

- [ ] **Step 5: Commit**

```bash
git add app/db/models/audit.py alembic/versions/aa10_add_entered_url_to_audit_reports.py
git commit -m "feat(auditor): add entered_url column to audit_reports"
```

---

## Task 2: Safe bounded redirect resolver

**Files:**
- Modify: `app/auditor/discovery.py` (add near `_default_fetch_text`, ~line 178)
- Test: `tests/auditor/test_redirect_resolver.py`

**Interfaces:**
- Consumes: `resolve_and_validate_host` (from `app.utils.network_security`), `normalize_url` (this module).
- Produces:
  - `class ResolvedRoot` — frozen dataclass with `final_url: str`, `hops: tuple[str, ...]`, `changed: bool`.
  - `async def resolve_redirect_chain(url: str, *, max_hops: int = 3, http_get=None) -> ResolvedRoot` — follows the chain, re-guarding every hop; raises `ValueError` on non-http(s), unsafe host, loop, or exceeding `max_hops`. `http_get` is an injectable `async (url) -> tuple[int, str | None]` returning `(status_code, location_header)` for tests.

- [ ] **Step 1: Write the failing tests**

Create `tests/auditor/test_redirect_resolver.py`:

```python
import socket
import pytest

from app.auditor.discovery import resolve_redirect_chain, ResolvedRoot


def _chain(*pairs):
    """Build a fake http_get from {url: (status, location)} pairs."""
    table = dict(pairs)

    async def _get(url):
        if url not in table:
            raise AssertionError(f"unexpected GET {url}")
        return table[url]

    return _get


@pytest.mark.asyncio
async def test_no_redirect_returns_same_url_unchanged():
    get = _chain(("https://example.com", (200, None)))
    r = await resolve_redirect_chain("https://example.com", http_get=get)
    assert isinstance(r, ResolvedRoot)
    assert r.final_url == "https://example.com"
    assert r.changed is False


@pytest.mark.asyncio
async def test_apex_to_www_is_followed():
    get = _chain(
        ("https://luranai.com", (307, "https://www.luranai.com/")),
        ("https://www.luranai.com/", (200, None)),
    )
    r = await resolve_redirect_chain("https://luranai.com", http_get=get)
    assert r.final_url == "https://www.luranai.com/"
    assert r.changed is True


@pytest.mark.asyncio
async def test_redirect_loop_raises():
    get = _chain(
        ("https://a.com", (302, "https://b.com")),
        ("https://b.com", (302, "https://a.com")),
    )
    with pytest.raises(ValueError):
        await resolve_redirect_chain("https://a.com", http_get=get)


@pytest.mark.asyncio
async def test_exceeding_max_hops_raises():
    get = _chain(
        ("https://a.com", (302, "https://b.com")),
        ("https://b.com", (302, "https://c.com")),
        ("https://c.com", (302, "https://d.com")),
        ("https://d.com", (302, "https://e.com")),
    )
    with pytest.raises(ValueError):
        await resolve_redirect_chain("https://a.com", max_hops=3, http_get=get)


@pytest.mark.asyncio
async def test_internal_hop_is_blocked(monkeypatch):
    # Default resolver (no injected http_get) must re-guard each hop. Point the
    # SSRF guard at a link-local target and confirm it raises.
    from app.utils import network_security as ns

    def fake_resolve(host, *a, **k):
        if host == "internal.example":
            raise ValueError("URL resolves to disallowed IP: 169.254.169.254")
        return "93.184.216.34"

    monkeypatch.setattr("app.auditor.discovery.resolve_and_validate_host", fake_resolve)
    get = _chain(("https://internal.example", (200, None)))
    with pytest.raises(ValueError):
        await resolve_redirect_chain("https://internal.example", http_get=get)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run selector: `tests/auditor/test_redirect_resolver.py`
Expected: FAIL with `ImportError: cannot import name 'resolve_redirect_chain'`.

- [ ] **Step 3: Implement the resolver**

In `app/auditor/discovery.py`, add `from dataclasses import dataclass` to the imports if absent, and add after `_default_fetch_text` (after line 178):

```python
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}


@dataclass(frozen=True)
class ResolvedRoot:
    """Outcome of following a root URL's redirect chain."""

    final_url: str
    hops: tuple[str, ...]
    changed: bool


async def _guarded_get(url: str) -> tuple[int, str | None]:
    """GET one URL with the SSRF/DNS-rebind pin, following NO redirects.

    Returns (status_code, Location header or None). Raises ValueError if the
    host is unsafe (via resolve_and_validate_host). Mirrors the pinning contract
    in _default_fetch_text.
    """
    parsed = urlparse(url)
    original_host = parsed.hostname or ""
    safe_ip = resolve_and_validate_host(original_host)  # raises ValueError if unsafe
    host_for_header = f"[{original_host}]" if ":" in original_host else original_host
    host_header = f"{host_for_header}:{parsed.port}" if parsed.port else host_for_header
    connect_host = safe_ip if safe_ip != original_host else original_host
    pinned_url = httpx.URL(url).copy_with(host=connect_host)
    req = httpx.Request("GET", pinned_url, headers={"Host": host_header})
    if connect_host != original_host:
        req.extensions["sni_hostname"] = original_host
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
        resp = await client.send(req)
    return resp.status_code, resp.headers.get("location")


async def resolve_redirect_chain(
    url: str, *, max_hops: int = 3, http_get=None
) -> ResolvedRoot:
    """Follow the root URL's redirect chain safely and return the final URL.

    The ONLY place the auditor follows redirects. Re-guards every hop from
    scratch (SSRF/DNS-rebind); the crawl fetch stays follow_redirects=False.

    Raises ValueError on: non-http(s) scheme, missing host, an unsafe hop
    (propagated from resolve_and_validate_host / _guarded_get), a redirect loop,
    or exceeding max_hops.
    """
    http_get = http_get or _guarded_get
    start = normalize_url(url)
    if not start:
        raise ValueError("Invalid URL")
    current = start
    seen: set[str] = set()
    hops: list[str] = [current]
    for _ in range(max_hops + 1):
        parsed = urlparse(current)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("Only http/https URLs are allowed")
        if not parsed.hostname:
            raise ValueError("URL hostname is required")
        status, location = await http_get(current)
        if status not in _REDIRECT_STATUSES or not location:
            return ResolvedRoot(final_url=current, hops=tuple(hops), changed=current != start)
        nxt = normalize_url(urljoin(current, location))
        if not nxt:
            raise ValueError("Invalid redirect target")
        if nxt in seen:
            raise ValueError("Redirect loop")
        seen.add(current)
        current = nxt
        hops.append(current)
    raise ValueError("Too many redirects")
```

Confirm `urljoin` and `urlparse` are already imported at the top of `discovery.py` (they are — used by existing code). `httpx` is imported. `resolve_and_validate_host` is imported.

- [ ] **Step 4: Run the tests to verify they pass**

Run selector: `tests/auditor/test_redirect_resolver.py`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add app/auditor/discovery.py tests/auditor/test_redirect_resolver.py
git commit -m "feat(auditor): safe bounded redirect resolver (re-guards every hop)"
```

---

## Task 3: Redirect tier classifier

**Files:**
- Modify: `app/auditor/discovery.py` (add after `resolve_redirect_chain`)
- Test: `tests/auditor/test_redirect_classifier.py`

**Interfaces:**
- Produces: `def classify_redirect(entered_url: str, final_url: str) -> str` returning `"same_site"` (only `www.`/scheme differ, same port) or `"different_host"`.

- [ ] **Step 1: Write the failing tests**

Create `tests/auditor/test_redirect_classifier.py`:

```python
import pytest

from app.auditor.discovery import classify_redirect


@pytest.mark.parametrize(
    "entered, final, expected",
    [
        ("https://luranai.com", "https://www.luranai.com/", "same_site"),
        ("https://www.x.com", "https://x.com/", "same_site"),
        ("http://x.com", "https://x.com/", "same_site"),
        ("https://x.com", "https://x.com/en", "same_site"),   # path only, host equal
        ("https://x.com", "https://app.x.com/", "different_host"),
        ("https://x.com", "https://y.com/", "different_host"),
        ("https://x.com", "https://another.example.com/", "different_host"),
    ],
)
def test_classify_redirect(entered, final, expected):
    assert classify_redirect(entered, final) == expected
```

- [ ] **Step 2: Run to verify failure**

Run selector: `tests/auditor/test_redirect_classifier.py`
Expected: FAIL with `ImportError: cannot import name 'classify_redirect'`.

- [ ] **Step 3: Implement the classifier**

In `app/auditor/discovery.py`, add after `resolve_redirect_chain`:

```python
def _same_registrable_host(a: str, b: str) -> bool:
    """True if two hosts differ only by a leading www. (case-insensitive)."""
    return a.lower().removeprefix("www.") == b.lower().removeprefix("www.")


def classify_redirect(entered_url: str, final_url: str) -> str:
    """Classify entered→final for consent gating.

    "same_site": hosts differ only by a leading www. (scheme/path may also
    differ), same port — followed silently.
    "different_host": any other host change — requires explicit confirmation.
    """
    ei, fi = urlparse(entered_url), urlparse(final_url)
    same_host = _same_registrable_host(ei.hostname or "", fi.hostname or "")
    same_port = (ei.port or None) == (fi.port or None)
    return "same_site" if same_host and same_port else "different_host"
```

- [ ] **Step 4: Run to verify pass**

Run selector: `tests/auditor/test_redirect_classifier.py`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add app/auditor/discovery.py tests/auditor/test_redirect_classifier.py
git commit -m "feat(auditor): classify redirect as same-site vs different-host"
```

---

## Task 4: Canonicalization + confirm gating in `start_audit`

**Files:**
- Modify: `app/auditor/service.py:14` (imports), `:69-113` (`start_audit`)
- Test: `tests/auditor/test_start_audit_redirect.py`

**Interfaces:**
- Consumes: `resolve_redirect_chain`, `classify_redirect`, `normalize_url` (from `app.auditor.discovery`).
- Produces:
  - `class RedirectConfirmationRequired(Exception)` with attributes `entered: str`, `target: str`.
  - `AuditService.start_audit(..., confirmed_target: str | None = None)` — same-site/no-redirect → report with `url=final`, `entered_url` set when changed; different-host without a matching `confirmed_target` → raises `RedirectConfirmationRequired`; unsafe → `ValueError`.

- [ ] **Step 1: Write the failing tests**

Create `tests/auditor/test_start_audit_redirect.py`:

```python
import pytest
from unittest.mock import patch

from app.auditor.service import AuditService, RedirectConfirmationRequired
from app.auditor.discovery import ResolvedRoot
from app.db.models.audit import AuditScope, AuditReport


def _resolved(final, changed):
    async def _fake(url, **kwargs):
        return ResolvedRoot(final_url=final, hops=(url, final), changed=changed)
    return _fake


@pytest.mark.asyncio
async def test_apex_to_www_scans_canonical_and_records_entered(db_session):
    svc = AuditService(db_session)
    with patch("app.auditor.service.resolve_and_validate_host", return_value="93.184.216.34"), \
         patch("app.auditor.service.resolve_redirect_chain",
               new=_resolved("https://www.luranai.com/", True)), \
         patch("app.auditor.service.enqueue_audit", new=lambda _id: _noop()):
        report = await svc.start_audit(
            url="https://luranai.com", scope=AuditScope.PUBLIC, tenant_id=None
        )
    fresh = db_session.get(AuditReport, report.id)
    assert fresh.url == "https://www.luranai.com/"
    assert fresh.entered_url == "https://luranai.com"


@pytest.mark.asyncio
async def test_different_host_requires_confirmation(db_session):
    svc = AuditService(db_session)
    with patch("app.auditor.service.resolve_and_validate_host", return_value="93.184.216.34"), \
         patch("app.auditor.service.resolve_redirect_chain",
               new=_resolved("https://newco.io/", True)):
        with pytest.raises(RedirectConfirmationRequired) as ei:
            await svc.start_audit(
                url="https://oldco.com", scope=AuditScope.PUBLIC, tenant_id=None
            )
    assert ei.value.target == "https://newco.io/"
    # No report row was created.
    assert db_session.query(AuditReport).count() == 0


@pytest.mark.asyncio
async def test_matching_confirmed_target_proceeds(db_session):
    svc = AuditService(db_session)
    with patch("app.auditor.service.resolve_and_validate_host", return_value="93.184.216.34"), \
         patch("app.auditor.service.resolve_redirect_chain",
               new=_resolved("https://newco.io/", True)), \
         patch("app.auditor.service.enqueue_audit", new=lambda _id: _noop()):
        report = await svc.start_audit(
            url="https://oldco.com", scope=AuditScope.PUBLIC, tenant_id=None,
            confirmed_target="https://newco.io/",
        )
    assert db_session.get(AuditReport, report.id).url == "https://newco.io/"


@pytest.mark.asyncio
async def test_stale_confirmed_target_reprompts(db_session):
    svc = AuditService(db_session)
    with patch("app.auditor.service.resolve_and_validate_host", return_value="93.184.216.34"), \
         patch("app.auditor.service.resolve_redirect_chain",
               new=_resolved("https://newco.io/", True)):
        with pytest.raises(RedirectConfirmationRequired):
            await svc.start_audit(
                url="https://oldco.com", scope=AuditScope.PUBLIC, tenant_id=None,
                confirmed_target="https://stale-target.example/",
            )


async def _noop():
    return None
```

Note: `enqueue_audit` is patched to a lambda returning a coroutine (`_noop()`); if the real symbol name/location differs, adjust the patch target to where `start_audit` imports it.

- [ ] **Step 2: Run to verify failure**

Run selector: `tests/auditor/test_start_audit_redirect.py`
Expected: FAIL with `ImportError: cannot import name 'RedirectConfirmationRequired'`.

- [ ] **Step 3: Implement in `service.py`**

Update imports at `app/auditor/service.py:14`:

```python
from app.auditor.discovery import classify_redirect, normalize_url, resolve_redirect_chain
```

Add the exception near the top of the module (after imports, before the class):

```python
class RedirectConfirmationRequired(Exception):
    """The entered URL redirects to a different host that the user must confirm
    before we scan it. Carries the entered URL and the resolved target."""

    def __init__(self, entered: str, target: str) -> None:
        self.entered = entered
        self.target = target
        super().__init__(f"{entered} redirects to {target}")
```

Change the `start_audit` signature and insert the canonicalization block after `_assert_target_auditable` (replace lines 69-91 through the `report =` construction):

```python
    async def start_audit(
        self,
        *,
        url: str,
        scope: AuditScope,
        tenant_id: UUID | None,
        created_by: UUID | None = None,
        confirmed_target: str | None = None,
    ) -> AuditReport:
        normalized = normalize_url(url)
        if not normalized:
            raise ValueError("Invalid URL")
        domain = urlparse(normalized).netloc

        await self._assert_target_auditable(normalized, domain)

        # Canonicalize host redirects (apex→www, scheme upgrade) and gate a
        # genuinely different host behind explicit confirmation. resolve_redirect
        # _chain re-guards every hop and raises ValueError on unsafe/loop targets.
        resolved = await resolve_redirect_chain(normalized)
        final_url = resolved.final_url
        if classify_redirect(normalized, final_url) == "different_host":
            if confirmed_target is None or normalize_url(confirmed_target) != final_url:
                raise RedirectConfirmationRequired(entered=normalized, target=final_url)

        final_domain = urlparse(final_url).netloc
        entered_url = normalized if final_url != normalized else None

        job = ProcessingJob(
            tenant_id=tenant_id,
            job_type=JobType.WEBSITE_AUDIT,
            input_data={"kind": "website_audit", "url": final_url},
            created_by=created_by,
        )
        self.db.add(job)
        self.db.flush()

        report = AuditReport(
            tenant_id=tenant_id,
            url=final_url,
            domain=final_domain,
            entered_url=entered_url,
            scope=scope,
            status=AuditStatus.QUEUED,
            processing_job_id=job.id,
            claim_token=secrets.token_urlsafe(24) if scope == AuditScope.PUBLIC else None,
        )
```

Leave the rest of `start_audit` (add/flush/commit, enqueue, error handling, `return report`) unchanged.

- [ ] **Step 4: Run to verify pass**

Run selector: `tests/auditor/test_start_audit_redirect.py`
Expected: 4 passed. If the `enqueue_audit` patch target is wrong, fix it per the note in Step 1 and re-run.

- [ ] **Step 5: Run the whole auditor suite (no regressions)**

Run selector: `tests/auditor/`
Expected: all pass. The autouse fixture in `tests/auditor/conftest.py` patches `resolve_and_validate_host` in `service`; any test that now also exercises `resolve_redirect_chain` without patching it will hit real network — if a pre-existing auditor test breaks for that reason, patch `app.auditor.service.resolve_redirect_chain` in that test to a no-redirect `ResolvedRoot`, do not weaken the fixture.

- [ ] **Step 6: Commit**

```bash
git add app/auditor/service.py tests/auditor/test_start_audit_redirect.py
git commit -m "feat(auditor): canonicalize host redirects; confirm different-host before scan"
```

---

## Task 5: Endpoint wiring — 409 contract + expose entered_url

**Files:**
- Modify: `app/schemas/auditor.py:8-10,43-45` (add `confirmed_target`)
- Modify: `app/api/v1/endpoints/public_auditor.py:53-77` (409 mapping, pass `confirmed_target`) and the report GET (`:78-110`) to expose `entered_url`/`pages_scanned`
- Modify: `app/api/v1/endpoints/auditor.py:84-114` (409 mapping, pass `confirmed_target`)
- Test: `tests/auditor/test_public_auditor_redirect.py`

**Interfaces:**
- Consumes: `RedirectConfirmationRequired` (from `app.auditor.service`).
- Produces: both `POST /audit` endpoints accept optional `confirmed_target`; a different-host redirect returns HTTP 409 body `{"success": false, "error": {"code": "REDIRECT_CONFIRMATION_REQUIRED", "message": ..., "details": {"entered_url": ..., "redirect_target": ...}}}`.

- [ ] **Step 1: Write the failing test**

Create `tests/auditor/test_public_auditor_redirect.py`:

```python
import pytest
from unittest.mock import patch

from app.auditor.discovery import ResolvedRoot
from app.auditor.service import RedirectConfirmationRequired


def _resolved(final, changed):
    async def _fake(url, **kwargs):
        return ResolvedRoot(final_url=final, hops=(url, final), changed=changed)
    return _fake


def test_different_host_redirect_returns_409(client):
    with patch("app.auditor.service.resolve_and_validate_host", return_value="93.184.216.34"), \
         patch("app.auditor.service.resolve_redirect_chain",
               new=_resolved("https://newco.io/", True)):
        resp = client.post("/api/v1/auditor/audit", json={"url": "https://oldco.com"})
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"]["code"] == "REDIRECT_CONFIRMATION_REQUIRED"
    assert body["error"]["details"]["redirect_target"] == "https://newco.io/"


def test_apex_to_www_returns_202(client):
    with patch("app.auditor.service.resolve_and_validate_host", return_value="93.184.216.34"), \
         patch("app.auditor.service.resolve_redirect_chain",
               new=_resolved("https://www.luranai.com/", True)), \
         patch("app.auditor.service.enqueue_audit", new=lambda _id: _n()):
        resp = client.post("/api/v1/auditor/audit", json={"url": "https://luranai.com"})
    assert resp.status_code == 202


async def _n():
    return None
```

Confirm the public auditor route prefix with `grep -rn "prefix=" app/api/v1/api.py | grep -i audit`; use the exact path in the test (`/api/v1/auditor/audit` is the assumed prefix — correct it if the router prefix differs). Use whatever `client` fixture the existing endpoint tests use (see `tests/auditor/` for the fixture name; if endpoint tests use `test_client`, rename accordingly).

- [ ] **Step 2: Run to verify failure**

Run selector: `tests/auditor/test_public_auditor_redirect.py`
Expected: FAIL — `test_different_host_redirect_returns_409` gets 202 (exception not yet mapped).

- [ ] **Step 3: Add `confirmed_target` to the schemas**

In `app/schemas/auditor.py`:

```python
class PublicAuditRequest(BaseModel):
    url: HttpUrl
    confirmed_target: str | None = None


class DashboardAuditRequest(BaseModel):
    url: HttpUrl
    confirmed_target: str | None = None
```

- [ ] **Step 4: Map the exception and pass the flag (public endpoint)**

In `app/api/v1/endpoints/public_auditor.py`, import the exception:

```python
from app.auditor.service import AuditService, RedirectConfirmationRequired
```

Update the `start_public_audit` try/except:

```python
    try:
        report = await AuditService(db).start_audit(
            url=str(payload.url),
            scope=AuditScope.PUBLIC,
            tenant_id=None,
            confirmed_target=payload.confirmed_target,
        )
    except RedirectConfirmationRequired as exc:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "success": False,
                "error": {
                    "code": "REDIRECT_CONFIRMATION_REQUIRED",
                    "message": f"{exc.entered} redirects to {exc.target}. Scan {exc.target}?",
                    "details": {"entered_url": exc.entered, "redirect_target": exc.target},
                },
            },
        )
    except ValueError as exc:
        return _err("INVALID_URL", str(exc), status.HTTP_400_BAD_REQUEST)
    except RuntimeError:
        return _err(
            "ENQUEUE_FAILED",
            "Audit could not be queued. Please try again later.",
            status.HTTP_503_SERVICE_UNAVAILABLE,
        )
```

- [ ] **Step 5: Map the exception and pass the flag (dashboard endpoint)**

In `app/api/v1/endpoints/auditor.py`, import `RedirectConfirmationRequired` from `app.auditor.service`, pass `confirmed_target=payload.confirmed_target` into `start_audit`, and add before the `except ValueError`:

```python
    except RedirectConfirmationRequired as exc:
        return _err(
            "REDIRECT_CONFIRMATION_REQUIRED",
            f"{exc.entered} redirects to {exc.target}. Scan {exc.target}?",
            status.HTTP_409_CONFLICT,
            details={"entered_url": exc.entered, "redirect_target": exc.target},
        )
```

(`_err` in `auditor.py` already accepts a `details` argument — see `auditor.py:25`.)

- [ ] **Step 6: Expose `entered_url` and `pages_scanned` in the report responses**

In `app/api/v1/endpoints/public_auditor.py`'s report GET handler, add `entered_url` and `pages_scanned` to the serialized response dict (alongside the existing `url`/scores). If the response is built via a Pydantic model (`PublicReportResponse`), add:

```python
    entered_url: str | None = None
    pages_scanned: int = 0
```

to that model in `app/schemas/auditor.py` and populate from the report row. Match the existing serialization style in the handler (dict vs model) — grep the handler for how `url` is currently returned and mirror it exactly for the two new fields.

- [ ] **Step 7: Run the tests to verify they pass**

Run selector: `tests/auditor/test_public_auditor_redirect.py`
Expected: 2 passed.

- [ ] **Step 8: Commit**

```bash
git add app/schemas/auditor.py app/api/v1/endpoints/public_auditor.py app/api/v1/endpoints/auditor.py tests/auditor/test_public_auditor_redirect.py
git commit -m "feat(auditor): 409 redirect-confirmation contract; expose entered_url/pages_scanned"
```

---

## Task 6: Public scan credit consumed only on enqueue; generous anti-abuse limiter

**Files:**
- Modify: `app/api/v1/endpoints/public_auditor.py:22-30` (limiters) and `start_public_audit` handler
- Test: extend `tests/auditor/test_public_auditor_redirect.py`

**Interfaces:**
- Consumes: `RedisRateLimiter`, `create_rate_limit_dependency`, `public_audit_limiter` (existing 3/day), `RedirectConfirmationRequired`.
- Produces: the endpoint dependency is a generous per-IP anti-abuse limiter (30/day); the 3/day scan credit is checked before work and consumed only when a report is actually enqueued.

- [ ] **Step 1: Write the failing test**

Append to `tests/auditor/test_public_auditor_redirect.py`:

```python
def test_confirmation_does_not_spend_scan_credit(client):
    calls = {"acquire": 0}

    async def _count_acquire(identifier="default"):
        calls["acquire"] += 1

    async def _usage(identifier="default"):
        return {"calls": 0, "remaining": 3}

    with patch("app.auditor.service.resolve_and_validate_host", return_value="93.184.216.34"), \
         patch("app.auditor.service.resolve_redirect_chain",
               new=_resolved("https://newco.io/", True)), \
         patch("app.api.v1.endpoints.public_auditor.public_audit_limiter.acquire", new=_count_acquire), \
         patch("app.api.v1.endpoints.public_auditor.public_audit_limiter.get_current_usage", new=_usage):
        resp = client.post("/api/v1/auditor/audit", json={"url": "https://oldco.com"})
    assert resp.status_code == 409
    assert calls["acquire"] == 0  # no scan credit spent on a confirmation prompt
```

- [ ] **Step 2: Run to verify failure**

Run selector: `tests/auditor/test_public_auditor_redirect.py::test_confirmation_does_not_spend_scan_credit`
Expected: FAIL — the pre-request dependency currently calls `acquire`, so `calls["acquire"]` is 1.

- [ ] **Step 3: Split the limiter and move the credit to enqueue**

In `app/api/v1/endpoints/public_auditor.py`, add a generous anti-abuse limiter next to the existing `public_audit_limiter` (keep `public_audit_limiter` as the 3/day scan credit, but stop using it as the endpoint dependency):

```python
# 3/day: the product "scan credit" — consumed only when an audit is enqueued.
public_audit_limiter = RedisRateLimiter(
    max_calls=3, window_seconds=86400, key_prefix="public_audit"
)
# Generous per-IP anti-abuse guard on the endpoint itself — covers every attempt
# (incl. confirmation round-trips and redirect pre-flights) so the pre-flight is
# never an unmetered probe. Distinct from the scan credit above.
public_audit_abuse_limiter = RedisRateLimiter(
    max_calls=30, window_seconds=86400, key_prefix="public_audit_abuse"
)
public_audit_rate_limit = create_rate_limit_dependency(public_audit_abuse_limiter, "ip")
```

(Confirm `RedisRateLimiter.__init__` accepts `key_prefix`; if the existing `public_audit_limiter` did not pass one, keep it exactly as it was and only add the new abuse limiter + swap the dependency.)

Change the handler to take `request: Request`, pre-check the scan credit, and consume it only after a successful enqueue:

```python
from fastapi import APIRouter, Depends, Request, status

async def start_public_audit(
    payload: PublicAuditRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    ip = request.client.host if request.client else "unknown"
    usage = await public_audit_limiter.get_current_usage(ip)
    if usage["calls"] >= public_audit_limiter.max_calls:
        return _err(
            "AUDIT_LIMIT_REACHED",
            "You've used your free scans for today. They reset at midnight UTC.",
            status.HTTP_429_TOO_MANY_REQUESTS,
        )
    try:
        report = await AuditService(db).start_audit(
            url=str(payload.url),
            scope=AuditScope.PUBLIC,
            tenant_id=None,
            confirmed_target=payload.confirmed_target,
        )
    except RedirectConfirmationRequired as exc:
        return JSONResponse(  # no scan credit spent
            status_code=status.HTTP_409_CONFLICT,
            content={
                "success": False,
                "error": {
                    "code": "REDIRECT_CONFIRMATION_REQUIRED",
                    "message": f"{exc.entered} redirects to {exc.target}. Scan {exc.target}?",
                    "details": {"entered_url": exc.entered, "redirect_target": exc.target},
                },
            },
        )
    except ValueError as exc:
        return _err("INVALID_URL", str(exc), status.HTTP_400_BAD_REQUEST)
    except RuntimeError:
        return _err(
            "ENQUEUE_FAILED",
            "Audit could not be queued. Please try again later.",
            status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    await public_audit_limiter.acquire(ip)  # real scan → spend one credit
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={
            "success": True,
            "data": {"audit_report_id": str(report.id), "claim_token": report.claim_token},
        },
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run selector: `tests/auditor/test_public_auditor_redirect.py`
Expected: all pass (3 tests). Then run `tests/auditor/` — expect the existing public-audit rate-limit test to still pass; if it asserted the old 3/day dependency behavior on the endpoint, update it to drive 3 successful *enqueues* (not 3 arbitrary POSTs) to hit the credit, since credits now attach to enqueues. Do not silently delete coverage — adjust it to the new contract.

- [ ] **Step 5: Ruff + full auditor suite**

Run ruff on `app/` and `tests/auditor/`. Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add app/api/v1/endpoints/public_auditor.py tests/auditor/test_public_auditor_redirect.py
git commit -m "feat(auditor): spend public scan credit only on enqueue; add anti-abuse limiter"
```

---

## Self-Review (author)

- **Spec coverage:** resolver (Task 2), tiers (Task 3), silent same-site + confirm different-host + re-resolve/compare (Task 4), 409 contract on both endpoints (Task 5), `entered_url` + `pages_scanned` transparency (Tasks 1, 5), scan-credit-on-enqueue + anti-abuse limiter (Task 6), SSRF re-guard every hop (Task 2, enforced in `_guarded_get`). Thin-crawl safety net is unchanged existing behavior — no task needed. Frontend confirm modal + scanned-URL display are explicitly a separate plan (spec rollout: backend lands first; Tier 2 surfaces an actionable 409 until the modal ships).
- **Type consistency:** `resolve_redirect_chain -> ResolvedRoot(final_url, hops, changed)`; `classify_redirect -> "same_site"|"different_host"`; `RedirectConfirmationRequired(entered, target)`; `start_audit(..., confirmed_target=None)` — used identically across Tasks 4–6.
- **Known verification points for the implementer (not placeholders):** the exact `enqueue_audit` patch target (Task 4 Step 1 note), the public router prefix and `client` fixture name (Task 5 Step 1 note), and whether `RedisRateLimiter.__init__` takes `key_prefix` (Task 6 Step 3 note). Each has an explicit check instruction.

## Out of scope (separate frontend plan)

- Confirm modal on the public auditor and dashboard auditor consuming the 409 `REDIRECT_CONFIRMATION_REQUIRED` body and re-submitting with `confirmed_target`.
- "Scanned www.luranai.com (you entered luranai.com) · N pages reached" display from `entered_url` + `pages_scanned`.
