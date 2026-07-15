"""Depth is a language-independent signal: a top-level page outranks one buried
in a product family. This is what makes the budget sane for a site whose paths
we cannot read — /tarifs, /aide, /kontakt match no keyword, but they are still
depth-1 and still beat /produits/a/b."""

import pytest

from app.auditor import discovery


def _sitemap(*paths):
    return "<urlset>" + "".join(f"<url><loc>https://ex.com{p}</loc></url>" for p in paths) + "</urlset>"


def _fetcher(xml):
    async def f(url):
        return xml if url.endswith("/sitemap.xml") else None
    return f


@pytest.mark.asyncio
async def test_shallow_pages_beat_deep_ones_without_any_keyword(monkeypatch):
    """A non-English site: no path matches the high-signal list, so only depth
    can spread the budget. Top-level pages must win."""
    monkeypatch.setattr(discovery, "guard_url", lambda u: True)
    xml = _sitemap(
        "/",
        "/produits/chat/fonctionnalites",
        "/produits/chat/integrations",
        "/produits/ventes/apercu",
        "/produits/ventes/tarification",
        "/tarifs",
        "/aide",
        "/kontakt",
    )

    found = await discovery.seed_urls("https://ex.com", max_pages=4, fetch_text=_fetcher(xml))

    for must in ("/tarifs", "/aide"):
        assert f"https://ex.com{must}" in found, (
            f"{must} is top-level and lost to a deep product tree; got {found}"
        )
