"""seed_urls must spend its page budget on the pages that decide the score.

Reproduces a real audit of stage.luranai.com: 10 pages scanned, and the budget
went entirely on the homepage, /pricing, /platform, /auditor and five sibling
/solutions/* pages. /faq, /contact and /security were never fetched — so the
report listed them as gaps. The site has all three.

seed_urls truncated the sitemap in document order (found[:max_pages]), so a site
that lists a large product family before its FAQ loses the FAQ to the cap. The
scorer then reads "no faq kind found" as "no FAQ exists", and the public auditor
tells a prospect their site is missing pages it has.

Ranking before truncating is the fix: the root first, then the high-signal pages
that map onto the score's dimensions, then everything else in sitemap order.
"""

import pytest

from app.auditor import discovery


def _sitemap(*paths: str) -> str:
    locs = "".join(f"<url><loc>https://ex.com{p}</loc></url>" for p in paths)
    return f"<urlset>{locs}</urlset>"


def _fetcher(xml: str):
    async def fake_fetch(url):
        return xml if url.endswith("/sitemap.xml") else None

    return fake_fetch


@pytest.mark.asyncio
async def test_high_signal_pages_survive_a_tight_budget(monkeypatch):
    """The luranai.com case: a product family listed before /faq must not eat
    the whole budget."""
    monkeypatch.setattr(discovery, "guard_url", lambda u: True)
    xml = _sitemap(
        "/",
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
        "/about",
    )

    found = await discovery.seed_urls("https://ex.com", max_pages=10, fetch_text=_fetcher(xml))

    assert len(found) == 10
    for must in ("/faq", "/contact", "/security", "/pricing"):
        assert f"https://ex.com{must}" in found, (
            f"{must} decides a score dimension and was dropped for a sibling "
            f"product page; got {found}"
        )


@pytest.mark.asyncio
async def test_root_is_always_first(monkeypatch):
    """The root is the one page the scan-quality diagnosis depends on."""
    monkeypatch.setattr(discovery, "guard_url", lambda u: True)
    xml = _sitemap("/", "/faq", "/pricing")

    found = await discovery.seed_urls("https://ex.com", max_pages=3, fetch_text=_fetcher(xml))

    assert found[0] == "https://ex.com/"


@pytest.mark.asyncio
async def test_ordinary_pages_still_fill_remaining_budget(monkeypatch):
    """Prioritising must not mean excluding: with room to spare, the ordinary
    pages are still seeded, in sitemap order."""
    monkeypatch.setattr(discovery, "guard_url", lambda u: True)
    xml = _sitemap("/", "/solutions/a", "/solutions/b", "/faq")

    found = await discovery.seed_urls("https://ex.com", max_pages=10, fetch_text=_fetcher(xml))

    assert set(found) == {
        "https://ex.com/",
        "https://ex.com/solutions/a",
        "https://ex.com/solutions/b",
        "https://ex.com/faq",
    }
    # sitemap order preserved among equal-priority pages
    assert found.index("https://ex.com/solutions/a") < found.index("https://ex.com/solutions/b")


@pytest.mark.asyncio
async def test_a_site_with_no_high_signal_pages_is_unaffected(monkeypatch):
    """No special pages: behaviour is exactly as before — sitemap order, capped."""
    monkeypatch.setattr(discovery, "guard_url", lambda u: True)
    xml = _sitemap("/", "/a", "/b", "/c")

    found = await discovery.seed_urls("https://ex.com", max_pages=3, fetch_text=_fetcher(xml))

    assert found == ["https://ex.com/", "https://ex.com/a", "https://ex.com/b"]


@pytest.mark.asyncio
async def test_priority_does_not_bypass_the_ssrf_guard(monkeypatch):
    """A high-signal path must not become a way around the guard."""
    monkeypatch.setattr(discovery, "guard_url", lambda u: "/faq" not in u)
    xml = _sitemap("/", "/faq", "/pricing")

    found = await discovery.seed_urls("https://ex.com", max_pages=10, fetch_text=_fetcher(xml))

    assert "https://ex.com/faq" not in found
    assert "https://ex.com/pricing" in found


@pytest.mark.asyncio
async def test_priority_does_not_resurrect_pruned_urls(monkeypatch):
    """Structural prune still wins: /login is never a seed, however it is named."""
    monkeypatch.setattr(discovery, "guard_url", lambda u: True)
    xml = _sitemap("/", "/login", "/faq")

    found = await discovery.seed_urls("https://ex.com", max_pages=10, fetch_text=_fetcher(xml))

    assert "https://ex.com/login" not in found
    assert "https://ex.com/faq" in found
