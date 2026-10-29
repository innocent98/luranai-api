"""Registrable-domain matching for the referral fraud flags.

Two tenants "share a domain" when their websites reduce to the same *registrable domain*: the
public suffix plus one label (``shop.example.com.ng`` and ``www.example.com.ng`` both become
``example.com.ng``; ``alpha.com.ng`` and ``beta.com.ng`` stay different, ``com.ng`` alone is never
a match). The public-suffix list is the snapshot bundled inside the pinned ``tldextract`` wheel:
it is built with no suffix-list URLs and no on-disk cache, so nothing here ever touches the
network and the result only changes when the dependency is bumped. Private suffixes (``vercel.app``,
``github.io``...) are honoured, so two unrelated sites hosted on one platform do not match.

A host whose suffix is not in the snapshot (a TLD newer than the pinned list, a typo) reduces to
``None`` rather than a guess: a wrong boundary would either merge unrelated tenants or split one
owner, and a missing domain only costs a missed signal. Every function here is total on odd input
(returns ``None`` / skips the row, never raises) because it runs inside the payment webhook.
"""

import ipaddress
import re
from functools import lru_cache
from urllib.parse import urlsplit
from uuid import UUID

import tldextract
from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.audit import AuditReport
from app.db.models.knowledge import KnowledgeSource
from app.db.models.tenant import Tenant

_MAX_INPUT_LENGTH = 2048
# Letters/digits/hyphen labels (1-63 chars, no leading/trailing hyphen). Punycode is plain ASCII.
_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_HOST_RE = re.compile(rf"^{_LABEL}(?:\.{_LABEL})+$")


def _build_extractor() -> tldextract.TLDExtract:
    """An extractor that can only use the snapshot shipped in the wheel.

    ``suffix_list_urls=()`` disables fetching the live list, ``cache_dir=None`` disables the
    on-disk cache, and ``fallback_to_snapshot`` (default) makes the bundled list the sole source.
    """
    return tldextract.TLDExtract(
        suffix_list_urls=(),
        cache_dir=None,
        fallback_to_snapshot=True,
        include_psl_private_domains=True,
    )


@lru_cache(maxsize=1)
def _extractor() -> tldextract.TLDExtract:
    return _build_extractor()


def _hostname(value: str) -> str | None:
    """Lower-cased host of a URL or bare host: no scheme, userinfo, port, path, query, fragment."""
    candidate = value.strip()
    if not candidate or len(candidate) > _MAX_INPUT_LENGTH:
        return None
    if "://" not in candidate and not candidate.startswith("//"):
        candidate = f"//{candidate}"
    try:
        host = urlsplit(candidate).hostname
    except ValueError:  # unbalanced IPv6 brackets, invalid port...
        return None
    if not host:
        return None
    return host.rstrip(".") or None


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def registrable_domain(value: str | None) -> str | None:
    """Reduce a URL or bare host to its registrable domain, or ``None`` if it has none.

    Lower-cases and strips scheme, userinfo, port, path, query and a leading ``www.`` (any
    subdomain, in fact), then keeps the public suffix plus one label. Internationalised names are
    returned in punycode so the unicode and ``xn--`` spellings of one domain compare equal.
    ``None`` for: empty or non-string input, ``localhost``, IP literals (v4 and v6), single-label
    hosts, bare public suffixes (``com.ng``), unparseable input, and suffixes missing from the
    bundled list. Never raises.
    """
    if not isinstance(value, str):
        return None
    try:
        host = _hostname(value)
        if host is None or _is_ip(host):
            return None
        if not host.isascii():
            host = host.encode("idna").decode("ascii").lower()
        if not _HOST_RE.match(host):
            return None
        result = _extractor()(host)
        if not result.domain or not result.suffix:
            return None
        domain = result.top_domain_under_public_suffix
        return domain.lower() if domain else None
    except Exception:  # noqa: BLE001 - odd data must never break the caller (payment webhook)
        logger.warning("registrable_domain: could not parse a stored value; skipping it")
        return None


def tenant_domains_by_tenant(db: Session, tenant_id: UUID | None = None) -> dict[UUID, set[str]]:
    """Registrable domains per tenant, from the three places a tenant's website shows up.

    * ``audit_reports.domain`` of reports the tenant has *claimed* (``tenant_id`` set; an
      unclaimed public audit belongs to nobody yet),
    * ``knowledge_sources.source_url`` of url-category sources (uploaded files have no site),
    * ``tenants.widget_last_seen_host`` (where the embedded widget was last seen).

    With ``tenant_id`` only that tenant is returned (an entry only if it has a domain). Tenants
    are hard-deleted on account deletion, so there is no soft-deleted/anonymised tenant to skip.
    Unparseable or empty values are skipped.
    """
    report_q = select(AuditReport.tenant_id, AuditReport.domain).where(
        AuditReport.tenant_id.is_not(None), AuditReport.domain.is_not(None)
    )
    source_q = select(KnowledgeSource.tenant_id, KnowledgeSource.source_url).where(
        KnowledgeSource.source_category == "url", KnowledgeSource.source_url.is_not(None)
    )
    widget_q = select(Tenant.id, Tenant.widget_last_seen_host).where(
        Tenant.widget_last_seen_host.is_not(None)
    )
    if tenant_id is not None:
        report_q = report_q.where(AuditReport.tenant_id == tenant_id)
        source_q = source_q.where(KnowledgeSource.tenant_id == tenant_id)
        widget_q = widget_q.where(Tenant.id == tenant_id)

    found: dict[UUID, set[str]] = {}
    for query in (report_q, source_q, widget_q):
        for owner_id, raw in db.execute(query):
            domain = registrable_domain(raw)
            if domain:
                found.setdefault(owner_id, set()).add(domain)
    return found


def tenant_domains(db: Session, tenant_id: UUID) -> set[str]:
    """The tenant's set of registrable domains (empty if it has none)."""
    return tenant_domains_by_tenant(db, tenant_id).get(tenant_id, set())
