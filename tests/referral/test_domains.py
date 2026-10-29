"""Registrable-domain matching: pure normalisation, offline public-suffix lookups, tenant domains."""

import socket
from uuid import uuid4

import pytest

from app.db.models.audit import AuditReport, AuditScope, AuditStatus
from app.db.models.knowledge import KnowledgeSource, SourceType
from app.db.models.tenant import Tenant
from app.services.referral import domains
from app.services.referral.domains import (
    registrable_domain,
    tenant_domain_sources,
    tenant_domains,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://WWW.Shop.Example.com.ng:8080/x", "example.com.ng"),
        ("example.com", "example.com"),
        ("foo.co.uk", "foo.co.uk"),
        ("http://user:pw@www.Example.com/a/b?q=1#frag", "example.com"),
        ("  www.example.com/path  ", "example.com"),
        ("example.com:8080", "example.com"),
        ("example.com.", "example.com"),
        ("shop.example.com.ng", "example.com.ng"),
        ("www.example.com.ng", "example.com.ng"),
    ],
)
def test_registrable_domain(value, expected):
    assert registrable_domain(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "localhost",
        "http://localhost:8000/x",
        "192.168.1.1",
        "http://10.0.0.1:8080/x",
        "[::1]",
        "http://[2001:db8::1]:8080/x",
        "2001:db8::1",
        "",
        "   ",
        None,
        123,
        "not a host",
        "http://",
        "com",
        "com.ng",
        "co.uk",
        "http://[bad",
        "a" * 300 + ".com",
        "-bad-.com",
        "exa mple.com",
    ],
)
def test_registrable_domain_rejects_non_domains(value):
    assert registrable_domain(value) is None


def test_registrable_domain_unknown_tld_is_none_not_a_guess():
    # Not in the bundled suffix list: we refuse to guess a boundary (a wrong one would either
    # merge unrelated tenants or split one owner), so it contributes no domain.
    assert registrable_domain("shop.example.invalidtld") is None


def test_registrable_domain_private_suffixes_keep_tenants_apart():
    # Shared hosting: two unrelated sites on the same platform are not the same owner.
    assert registrable_domain("alpha.vercel.app") == "alpha.vercel.app"
    assert registrable_domain("beta.vercel.app") == "beta.vercel.app"
    assert registrable_domain("https://www.alpha.vercel.app/x") == "alpha.vercel.app"
    assert registrable_domain("vercel.app") is None


def test_registrable_domain_more_private_suffixes():
    assert registrable_domain("alpha.github.io") == "alpha.github.io"
    assert registrable_domain("https://beta.github.io/repo") == "beta.github.io"
    assert registrable_domain("alpha.myshopify.com") == "alpha.myshopify.com"
    assert registrable_domain("beta.myshopify.com") == "beta.myshopify.com"
    assert registrable_domain("github.io") is None
    assert registrable_domain("myshopify.com") is None


def test_registrable_domain_still_returns_non_ownership_domains():
    # Only the tenant-evidence builder skips these; the pure function does not.
    assert registrable_domain("https://www.instagram.com/shop") == "instagram.com"
    assert registrable_domain("wa.me/2340000000000") == "wa.me"


def test_registrable_domain_internationalised():
    unicode_form = registrable_domain("https://www.例子.中国/路径")
    puny_form = registrable_domain("xn--fsqu00a.xn--fiqs8s")
    assert unicode_form == puny_form == "xn--fsqu00a.xn--fiqs8s"
    # Odd unicode never raises.
    assert registrable_domain("\u0000‮.com") is None
    assert registrable_domain("exa\ud800mple.com") is None


def test_registrable_domain_never_touches_the_network(monkeypatch):
    import requests

    def _boom(*args, **kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket.socket, "connect", _boom)
    monkeypatch.setattr(socket, "create_connection", _boom)
    monkeypatch.setattr(socket, "getaddrinfo", _boom)
    monkeypatch.setattr(requests.Session, "request", _boom)

    # A brand-new extractor (not the cached module one) must also resolve offline.
    fresh = domains._build_extractor()
    assert fresh("shop.example.com.ng").top_domain_under_public_suffix == "example.com.ng"
    assert registrable_domain("https://shop.example.com.ng/x") == "example.com.ng"
    assert registrable_domain("foo.co.uk") == "foo.co.uk"


def test_extractor_is_configured_offline_only():
    extractor = domains._build_extractor()
    assert tuple(extractor.suffix_list_urls) == ()
    # No on-disk suffix cache either: nothing to fetch, refresh or write at runtime.
    assert extractor._cache.enabled is False
    assert extractor.fallback_to_snapshot is True


# --- tenant_domains -------------------------------------------------------------------------


def _tenant(db) -> Tenant:
    tenant = Tenant(
        name=f"t_{uuid4().hex[:8]}",
        display_name="Dom Tenant",
        api_key_hash=uuid4().hex,
        subscription_tier="trial",
        subscription_status="trialing",
    )
    db.add(tenant)
    db.flush()
    return tenant


def _report(db, tenant_id, domain, *, url=None) -> AuditReport:
    report = AuditReport(
        tenant_id=tenant_id,
        url=url or f"https://{domain}",
        domain=domain,
        scope=AuditScope.DASHBOARD,
        status=AuditStatus.COMPLETED,
    )
    db.add(report)
    db.flush()
    return report


def _source(db, tenant_id, url, *, category="url") -> KnowledgeSource:
    source = KnowledgeSource(
        tenant_id=tenant_id,
        name="src",
        source_type=SourceType.OTHER,
        source_category=category,
        source_url=url,
        content_hash=uuid4().hex,
    )
    db.add(source)
    db.flush()
    return source


def test_tenant_domains_collects_all_three_sources(db):
    tenant = _tenant(db)
    _report(db, tenant.id, "www.alpha-shop.example.com.ng")
    _source(db, tenant.id, "https://docs.beta-shop.example.com/help")
    tenant.widget_last_seen_host = "www.gamma-shop.example.org"
    db.flush()

    assert tenant_domains(db, tenant.id) == {
        "example.com.ng",
        "example.com",
        "example.org",
    }


def test_tenant_domains_ignores_unclaimed_reports_files_and_garbage(db):
    tenant = _tenant(db)
    other = _tenant(db)
    _report(db, None, "unclaimed.example.com")  # never claimed by anyone
    _report(db, other.id, "others.example.com")  # claimed by someone else
    _source(db, tenant.id, "https://file-upload.example.com/x", category="file")
    _source(db, tenant.id, None)
    _source(db, tenant.id, "")
    _source(db, tenant.id, "not a url")
    _source(db, tenant.id, "http://localhost:3000")
    _report(db, tenant.id, "10.0.0.1")
    tenant.widget_last_seen_host = "   "
    db.flush()

    assert tenant_domains(db, tenant.id) == set()


def test_tenant_domains_skips_non_ownership_domains(db):
    tenant = _tenant(db)
    _report(db, tenant.id, "instagram.com")
    _source(db, tenant.id, "https://www.facebook.com/page")
    tenant.widget_last_seen_host = "shop.example.com"
    db.flush()
    assert tenant_domains(db, tenant.id) == {"example.com"}


def test_tenant_domain_sources_maps_domain_to_sources(db):
    tenant = _tenant(db)
    _report(db, tenant.id, "example.com")
    _source(db, tenant.id, "https://example.com/a")
    _source(db, tenant.id, "https://example.com/a")
    tenant.widget_last_seen_host = "www.example.org"
    db.flush()
    assert tenant_domain_sources(db, tenant.id) == {
        tenant.id: {"example.com": {"audit", "knowledge_url"}, "example.org": {"widget"}}
    }


def test_tenant_domains_unknown_tenant_is_empty(db):
    assert tenant_domains(db, uuid4()) == set()
