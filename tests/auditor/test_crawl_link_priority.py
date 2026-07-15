"""crawl_site must spend its page budget on the pages that decide the score.

Ranking the sitemap seeds was not enough. Two audits of stage.luranai.com — one
before that change and one after the worker had loaded it — crawled the exact
same 9 pages and never fetched /faq, /contact or /security, which the site has.

The seeds were never the deciding factor:
  - The site's sitemap advertises 4 URLs and hardcodes the luranai.com host, so
    auditing stage.luranai.com same-host-filters every one of them away and the
    seeds collapse to the root.
  - From there crawl_site fills the budget itself, popping a FIFO deque of links
    in DOM order. The homepage nav lists /platform, /auditor and five
    /solutions/* pages before the footer's /faq — so the budget is gone by then.

Enqueueing high-signal links first fixes the path that actually chooses pages.
"""

import pytest

from app.auditor import crawler


def _nav_html(*paths: str) -> str:
    links = "".join(f'<a href="{p}">x</a>' for p in paths)
    return f"<html><nav>{links}</nav></html>"


@pytest.mark.asyncio
async def test_high_signal_links_are_crawled_before_a_product_family(monkeypatch):
    """The real luranai.com shape: nav lists the product family first, /faq and
    /contact last. A 5-page budget must still reach them."""
    monkeypatch.setattr("app.auditor.crawler.guard_url", lambda url: True)
    monkeypatch.setattr("app.auditor.discovery.guard_url", lambda url: True)

    root_html = _nav_html(
        "/platform",
        "/auditor",
        "/solutions/ai-support-agent",
        "/solutions/ai-sales",
        "/solutions/automation",
        "/solutions/flows",
        "/solutions/website-auditor",
        "/pricing",
        "/faq",
        "/contact",
        "/security",
    )

    async def renderer(url):
        return (200, root_html if url == "https://x.com/" else "<html>page</html>")

    pages = await crawler.crawl_site(
        ["https://x.com/"], max_pages=5, rps=1000, page_renderer=renderer
    )
    got = {p.url for p in pages}

    for must in ("/faq", "/contact", "/pricing"):
        assert f"https://x.com{must}" in got, (
            f"{must} decides a score dimension but lost the budget to a sibling "
            f"product page; crawled {sorted(got)}"
        )


@pytest.mark.asyncio
async def test_ordinary_links_still_crawled_when_budget_allows(monkeypatch):
    """Prioritising must not exclude — with room, everything is still crawled."""
    monkeypatch.setattr("app.auditor.crawler.guard_url", lambda url: True)
    monkeypatch.setattr("app.auditor.discovery.guard_url", lambda url: True)

    root_html = _nav_html("/solutions/a", "/faq", "/solutions/b")

    async def renderer(url):
        return (200, root_html if url == "https://x.com/" else "<html>page</html>")

    pages = await crawler.crawl_site(
        ["https://x.com/"], max_pages=50, rps=1000, page_renderer=renderer
    )
    got = {p.url for p in pages}

    assert got == {
        "https://x.com/",
        "https://x.com/faq",
        "https://x.com/solutions/a",
        "https://x.com/solutions/b",
    }


@pytest.mark.asyncio
async def test_the_root_is_still_crawled_first(monkeypatch):
    """Ranking applies to discovered links, never to the seeds' own order."""
    monkeypatch.setattr("app.auditor.crawler.guard_url", lambda url: True)
    monkeypatch.setattr("app.auditor.discovery.guard_url", lambda url: True)

    async def renderer(url):
        return (200, _nav_html("/faq") if url == "https://x.com/" else "<html>p</html>")

    pages = await crawler.crawl_site(
        ["https://x.com/"], max_pages=5, rps=1000, page_renderer=renderer
    )

    assert pages[0].url == "https://x.com/"


@pytest.mark.asyncio
async def test_priority_does_not_bypass_robots(monkeypatch):
    """A high-signal path must not become a way around robots.txt.

    A disallowed URL is still recorded as a page — that is how the crawl reports
    the block — but it must never be fetched: html stays None and the kind is
    ROBOTS.
    """
    monkeypatch.setattr("app.auditor.crawler.guard_url", lambda url: True)
    monkeypatch.setattr("app.auditor.discovery.guard_url", lambda url: True)

    async def renderer(url):
        return (200, _nav_html("/faq", "/pricing") if url == "https://x.com/" else "<html>p</html>")

    pages = await crawler.crawl_site(
        ["https://x.com/"],
        max_pages=10,
        rps=1000,
        page_renderer=renderer,
        robots_disallowed={"/faq"},
    )
    by_url = {p.url: p for p in pages}

    faq = by_url.get("https://x.com/faq")
    assert faq is not None, "the block should be recorded, not silently dropped"
    assert faq.error == "ROBOTS_BLOCKED"
    assert faq.html is None, "a disallowed page must never be fetched"
    assert by_url["https://x.com/pricing"].html is not None
