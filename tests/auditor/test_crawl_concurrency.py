"""Concurrent crawls must be bounded by memory, not by ARQ's job slots.

The worker runs max_jobs=5 asyncio tasks in one loop, and run_website_audit is
one of its functions, so five audits can start together — five Chromiums. One
measured 394 MiB in the worker container on the lightest page there is, against
a 1200m ceiling:

    1 audit   554 MiB   fits
    2 audits  948 MiB   fits
    3 audits 1342 MiB   OOM kill

An OOM here is not quiet: ARQ retries (max_tries=3), so the audit burns its
retries and reports "failed" to the tenant.

_browser_render_session is the single place a crawl launches a browser, so the
bound belongs there. These tests pin that it holds under concurrency and that
slots are returned even when a crawl raises.
"""

import asyncio
from unittest.mock import patch

import pytest

from app.auditor import crawler


class _ConcurrencyProbe:
    """Stands in for playwright, recording how many browsers coexist."""

    def __init__(self) -> None:
        self.live = 0
        self.peak = 0

    def __call__(self):  # async_playwright()
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    @property
    def chromium(self):
        return self

    async def launch(self, **_kw):
        self.live += 1
        self.peak = max(self.peak, self.live)
        probe = self

        class _Browser:
            async def close(self_inner):
                probe.live -= 1

            async def new_context(self_inner, **_k):
                # The session now opens one UA context per browser; these tests
                # exercise only launch/close accounting (never render a page), so
                # a benign context stub keeps the permit/peak assertions intact.
                class _Context:
                    async def new_page(self_inner):
                        raise AssertionError("no page render in these tests")

                    async def close(self_inner):
                        pass

                return _Context()

        # let other tasks interleave while this browser is open
        await asyncio.sleep(0.02)
        return _Browser()


async def _run_session(host: str = "example.com", ip: str = "93.184.216.34") -> None:
    async with crawler._browser_render_session(host, ip):
        await asyncio.sleep(0.02)


@pytest.mark.asyncio
async def test_concurrent_crawls_never_exceed_the_configured_limit():
    """Five audits starting at once — ARQ's max_jobs — must not put five
    Chromiums in memory at the same time."""
    probe = _ConcurrencyProbe()

    with patch.object(crawler, "async_playwright", probe), patch.object(
        crawler, "MAX_CONCURRENT_CRAWLS", 2
    ), patch.object(crawler, "_CRAWL_SLOTS", asyncio.Semaphore(2)):
        await asyncio.gather(*(_run_session() for _ in range(5)))

    assert probe.peak <= 2, f"{probe.peak} browsers coexisted; the cap is 2"
    assert probe.live == 0, "every browser must be closed"


@pytest.mark.asyncio
async def test_all_crawls_still_complete_when_throttled():
    """The cap must queue work, not drop it — all five still run."""
    probe = _ConcurrencyProbe()
    launched = 0

    real_launch = probe.launch

    async def counting_launch(**kw):
        nonlocal launched
        launched += 1
        return await real_launch(**kw)

    probe.launch = counting_launch  # type: ignore[method-assign]

    with patch.object(crawler, "async_playwright", probe), patch.object(
        crawler, "_CRAWL_SLOTS", asyncio.Semaphore(2)
    ):
        await asyncio.gather(*(_run_session() for _ in range(5)))

    assert launched == 5, "throttling must delay crawls, never skip them"


@pytest.mark.asyncio
async def test_a_failing_crawl_returns_its_slot():
    """A crawl that raises must not leak a permit, or the worker deadlocks after
    MAX_CONCURRENT_CRAWLS failures and every later audit hangs to job_timeout."""
    probe = _ConcurrencyProbe()
    sem = asyncio.Semaphore(2)

    async def _boom():
        async with crawler._browser_render_session("example.com", "93.184.216.34"):
            raise RuntimeError("crawl blew up mid-page")

    with patch.object(crawler, "async_playwright", probe), patch.object(
        crawler, "_CRAWL_SLOTS", sem
    ):
        for _ in range(3):
            with pytest.raises(RuntimeError):
                await _boom()

        # both permits must still be available
        assert sem._value == 2, f"leaked permits: {sem._value} of 2 left"
        await asyncio.wait_for(_run_session(), timeout=1.0)

    assert probe.live == 0


def test_limit_is_configurable_and_defaults_conservatively():
    """Default must fit the worker's ceiling: 2 crawls = 948 MiB under 1200m."""
    assert isinstance(crawler.MAX_CONCURRENT_CRAWLS, int)
    assert crawler.MAX_CONCURRENT_CRAWLS >= 1
    assert crawler.MAX_CONCURRENT_CRAWLS <= 3, (
        "more than 3 concurrent Chromiums exceeds WORKER_MEM_LIMIT=1200m "
        "(160 MiB base + 394 MiB each)"
    )
