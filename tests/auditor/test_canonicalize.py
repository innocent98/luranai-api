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
