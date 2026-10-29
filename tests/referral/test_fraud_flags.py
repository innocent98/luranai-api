"""Fraud flags: flag-and-hold signals only. None of them may ever block a signup or payment."""

from datetime import datetime, timedelta
from uuid import uuid4

from app.db.models.audit import AuditReport, AuditScope, AuditStatus
from app.db.models.knowledge import KnowledgeSource, SourceType
from app.db.models.payment import PaymentTransaction
from app.db.models.referral import (
    AttributionSource,
    Marketer,
    ReferralAttribution,
    SignupOrigin,
)
from app.db.models.tenant import Tenant
from app.services.referral.fraud import (
    SharedDomainMatch,
    evaluate_flags,
    shared_domain_tenants,
)

NOW = datetime(2026, 10, 1, 12, 0, 0)


def _tenant(db, display_name="Flag Tenant") -> Tenant:
    tenant = Tenant(
        name=f"t_{uuid4().hex[:8]}",
        display_name=display_name,
        api_key_hash=uuid4().hex,
        subscription_tier="trial",
        subscription_status="trialing",
    )
    db.add(tenant)
    db.flush()
    return tenant


def _marketer(db) -> Marketer:
    marketer = Marketer(
        name="Flag Marketer",
        email=f"m_{uuid4().hex[:6]}@marketer-co.example",
        code=f"code-{uuid4().hex[:8]}",
    )
    db.add(marketer)
    db.flush()
    return marketer


def _attribute(
    db, tenant, marketer, *, self_referral=False, at=NOW, origin=None
) -> ReferralAttribution:
    attribution = ReferralAttribution(
        tenant_id=tenant.id,
        marketer_id=marketer.id,
        source=AttributionSource.COOKIE,
        self_referral=self_referral,
        attributed_at=at,
    )
    db.add(attribution)
    if origin:
        db.add(SignupOrigin(tenant_id=tenant.id, origin_hash=origin))
    db.flush()
    return attribution


def _widget_host(db, tenant, host) -> None:
    tenant.widget_last_seen_host = host
    db.flush()


def _report(db, tenant, domain) -> None:
    db.add(
        AuditReport(
            tenant_id=tenant.id,
            url=f"https://{domain}",
            domain=domain,
            scope=AuditScope.DASHBOARD,
            status=AuditStatus.COMPLETED,
        )
    )
    db.flush()


def _url_source(db, tenant, url) -> None:
    db.add(
        KnowledgeSource(
            tenant_id=tenant.id,
            name="src",
            source_type=SourceType.OTHER,
            source_category="url",
            source_url=url,
            content_hash=uuid4().hex,
        )
    )
    db.flush()


def _payment(db, tenant, *, signature="SIG_fake_a", status="success", data="default"):
    """Stored as the webhook does: the whole ``data`` object of a charge.success event."""
    if data == "default":
        data = {
            "reference": f"ref_{uuid4().hex[:10]}",
            "amount": 500000,
            "status": "success",
            "channel": "card",
            "authorization": {
                "authorization_code": "AUTH_fake",
                "card_type": "visa",
                "last4": "0000",
                "reusable": True,
                "signature": signature,
            },
            "customer": {"email": "buyer@example.com"},
        }
    db.add(
        PaymentTransaction(
            tenant_id=tenant.id,
            reference=f"ref_{uuid4().hex[:12]}",
            amount=500000,
            status=status,
            tier="lite",
            billing_cycle="monthly",
            paystack_data=data,
        )
    )
    db.flush()


# --- shared_domain --------------------------------------------------------------------------


def test_tenants_with_www_and_apex_match(db):
    a, b = _tenant(db), _tenant(db)
    _report(db, a, "example.com")
    _report(db, b, "www.example.com")
    assert evaluate_flags(db, a.id) == ["shared_domain"]
    assert evaluate_flags(db, b.id) == ["shared_domain"]


def test_subdomain_vs_apex_match_on_com_ng(db):
    a, b = _tenant(db), _tenant(db)
    _url_source(db, a, "https://shop.example.com.ng/products")
    _widget_host(db, b, "www.example.com.ng")
    assert evaluate_flags(db, a.id) == ["shared_domain"]


def test_com_ng_alone_is_not_a_match_between_unrelated_tenants(db):
    a, b = _tenant(db), _tenant(db)
    _report(db, a, "alpha.com.ng")
    _report(db, b, "beta.com.ng")
    assert evaluate_flags(db, a.id) == []
    assert evaluate_flags(db, b.id) == []


def test_shared_domain_ignores_marketer_and_unattributed_tenants(db):
    # Compared against EVERY other tenant, attributed or not.
    marketer = _marketer(db)
    a, b = _tenant(db), _tenant(db)
    _attribute(db, a, marketer)
    _widget_host(db, a, "example.com")
    _widget_host(db, b, "example.com")
    assert evaluate_flags(db, a.id) == ["shared_domain"]
    assert evaluate_flags(db, b.id) == ["shared_domain"]


def test_different_domains_same_owner_not_flagged(db):
    # A person running several different businesses is legitimate: distinct domains, no flag.
    marketer = _marketer(db)
    a, b = _tenant(db), _tenant(db)
    _attribute(db, a, marketer)
    _attribute(db, b, marketer)
    _report(db, a, "bakery-one.example.com")
    _report(db, b, "garage-two.example.org")
    assert evaluate_flags(db, a.id) == []
    assert evaluate_flags(db, b.id) == []


def test_tenant_with_no_domains_yields_no_shared_domain(db):
    a, b = _tenant(db), _tenant(db)
    _report(db, b, "example.com")
    assert evaluate_flags(db, a.id) == []
    assert shared_domain_tenants(db, a.id) == []


def test_shared_domain_tenants_returns_names_sorted(db):
    me = _tenant(db, "Me Ltd")
    zed = _tenant(db, "Zed Stores")
    amy = _tenant(db, "Amy Bakes")
    other = _tenant(db, "Unrelated")
    _report(db, me, "www.example.com")
    _widget_host(db, me, "shop.example.org")
    _report(db, zed, "example.com")
    _url_source(db, amy, "https://example.org/x")
    _url_source(db, amy, "https://blog.example.com/x")
    _report(db, other, "elsewhere.example.net")

    assert shared_domain_tenants(db, me.id) == [
        SharedDomainMatch(amy.id, "Amy Bakes", "example.com", ("knowledge_url",), ("audit",)),
        SharedDomainMatch(amy.id, "Amy Bakes", "example.org", ("knowledge_url",), ("widget",)),
        SharedDomainMatch(zed.id, "Zed Stores", "example.com", ("audit",), ("audit",)),
    ]
    assert evaluate_flags(db, me.id) == ["shared_domain"]


def test_non_ownership_domains_never_cause_shared_domain(db):
    # Two unrelated tenants that only reference the same social / link-in-bio pages.
    a, b = _tenant(db), _tenant(db)
    for tenant in (a, b):
        _report(db, tenant, "instagram.com")
        _url_source(db, tenant, "https://www.facebook.com/some-page")
        _url_source(db, tenant, "https://linktr.ee/someone")
    _widget_host(db, a, "github.com")
    _widget_host(db, b, "github.com")
    assert evaluate_flags(db, a.id) == []
    assert shared_domain_tenants(db, a.id) == []

    # ... while a real shared website still flags.
    _widget_host(db, a, "shop.example.com.ng")
    _report(db, b, "www.example.com.ng")
    assert evaluate_flags(db, a.id) == ["shared_domain"]
    assert [m.domain for m in shared_domain_tenants(db, a.id)] == ["example.com.ng"]


def test_shared_domain_match_carries_sorted_evidence_sources(db):
    me, other = _tenant(db), _tenant(db)
    _widget_host(db, me, "www.example.com")
    _report(db, me, "example.com")
    _widget_host(db, other, "shop.example.com")
    _url_source(db, other, "https://example.com/help")
    _url_source(db, other, "https://example.com/help")  # duplicate raw url: still one source

    (match,) = shared_domain_tenants(db, me.id)
    assert match.sources == ("knowledge_url", "widget")
    assert match.own_sources == ("audit", "widget")


# --- shared_card ----------------------------------------------------------------------------


def test_shared_card_flagged_for_same_marketer(db):
    marketer = _marketer(db)
    a, b = _tenant(db), _tenant(db)
    _attribute(db, a, marketer)
    _attribute(db, b, marketer)
    _payment(db, a, signature="SIG_fake_same")
    _payment(db, b, signature="SIG_fake_same")
    assert evaluate_flags(db, a.id) == ["shared_card"]
    assert evaluate_flags(db, b.id) == ["shared_card"]


def test_shared_card_not_flagged_across_different_marketers(db):
    m1, m2 = _marketer(db), _marketer(db)
    a, b = _tenant(db), _tenant(db)
    _attribute(db, a, m1)
    _attribute(db, b, m2)
    _payment(db, a, signature="SIG_fake_same")
    _payment(db, b, signature="SIG_fake_same")
    assert evaluate_flags(db, a.id) == []


def test_shared_card_requires_attribution_and_success(db):
    marketer = _marketer(db)
    a, b, c, d = (_tenant(db) for _ in range(4))
    _attribute(db, a, marketer)
    _attribute(db, b, marketer)
    _attribute(db, c, marketer)
    _payment(db, a, signature="SIG_fake_same")
    _payment(db, b, signature="SIG_fake_same", status="failed")  # not a successful payment
    _payment(db, d, signature="SIG_fake_same")  # unattributed
    assert evaluate_flags(db, a.id) == []
    assert evaluate_flags(db, d.id) == []
    _payment(db, c, signature="SIG_fake_other")
    assert evaluate_flags(db, c.id) == []


def test_same_tenant_paying_twice_with_one_card_is_not_shared(db):
    marketer = _marketer(db)
    a = _tenant(db)
    _attribute(db, a, marketer)
    _payment(db, a, signature="SIG_fake_same")
    _payment(db, a, signature="SIG_fake_same")
    assert evaluate_flags(db, a.id) == []


def test_payload_without_signature_or_authorization_is_skipped_silently(db):
    marketer = _marketer(db)
    a, b = _tenant(db), _tenant(db)
    _attribute(db, a, marketer)
    _attribute(db, b, marketer)
    odd_payloads = [
        None,
        {},
        {"reference": "x"},
        {"authorization": None},
        {"authorization": "oops"},
        {"authorization": {"authorization_code": "AUTH_fake"}},
        {"authorization": {"signature": None}},
        {"authorization": {"signature": ""}},
        {"authorization": {"signature": 12345}},
        ["not", "a", "dict"],
    ]
    for payload in odd_payloads:
        _payment(db, a, data=payload)
        _payment(db, b, data=payload)
    assert evaluate_flags(db, a.id) == []
    assert evaluate_flags(db, b.id) == []


# --- shared_origin / self_referral ----------------------------------------------------------


def test_shared_origin_same_marketer_only(db):
    m1, m2 = _marketer(db), _marketer(db)
    a, b, c = _tenant(db), _tenant(db), _tenant(db)
    _attribute(db, a, m1, origin="o" * 64)
    _attribute(db, b, m1, origin="o" * 64)
    _attribute(db, c, m2, origin="o" * 64)
    assert evaluate_flags(db, a.id) == ["shared_origin"]
    assert evaluate_flags(db, c.id) == []


def test_self_referral_flag(db):
    marketer = _marketer(db)
    a = _tenant(db)
    _attribute(db, a, marketer, self_referral=True)
    assert evaluate_flags(db, a.id) == ["self_referral"]


# --- marketer_burst -------------------------------------------------------------------------


def _signups(db, marketer, count, *, end=NOW, spread_minutes=1):
    tenants = []
    for i in range(count):
        tenant = _tenant(db)
        _attribute(db, tenant, marketer, at=end - timedelta(minutes=spread_minutes * i))
        tenants.append(tenant)
    return tenants


def test_marketer_burst_boundary_ten_is_clean_eleven_is_flagged(db):
    marketer = _marketer(db)
    tenants = _signups(db, marketer, 10)
    newest = tenants[0]  # attributed at NOW; the window holds exactly 10 including itself
    assert evaluate_flags(db, newest.id) == []

    eleventh = _tenant(db)
    _attribute(db, eleventh, marketer, at=NOW + timedelta(minutes=1))
    assert evaluate_flags(db, eleventh.id) == ["marketer_burst"]


def test_marketer_burst_window_is_24h_before_this_attribution(db):
    marketer = _marketer(db)
    subject = _tenant(db)
    _attribute(db, subject, marketer, at=NOW)
    # 10 others: 9 inside the 24h window, 1 just outside it => 10 in window incl. subject.
    for i in range(9):
        _attribute(db, _tenant(db), marketer, at=NOW - timedelta(hours=1 + i))
    _attribute(db, _tenant(db), marketer, at=NOW - timedelta(hours=24, seconds=1))
    # later signups are not "before" this attribution
    _attribute(db, _tenant(db), marketer, at=NOW + timedelta(hours=1))
    assert evaluate_flags(db, subject.id) == []

    # exactly 24h before is inside the (inclusive) window: 11 in window => flagged
    _attribute(db, _tenant(db), marketer, at=NOW - timedelta(hours=24))
    assert evaluate_flags(db, subject.id) == ["marketer_burst"]


def test_marketer_burst_counts_only_that_marketer(db):
    m1, m2 = _marketer(db), _marketer(db)
    tenants = _signups(db, m1, 6)
    _signups(db, m2, 20)
    assert evaluate_flags(db, tenants[0].id) == []


# --- ordering / clean ----------------------------------------------------------------------


def test_each_flag_reason_returned_in_fixed_order(db):
    marketer = _marketer(db)
    subject = _tenant(db)
    twin = _tenant(db)
    _attribute(db, subject, marketer, self_referral=True, origin="p" * 64)
    _attribute(db, twin, marketer, origin="p" * 64, at=NOW - timedelta(minutes=5))
    _widget_host(db, subject, "www.example.com")
    _widget_host(db, twin, "example.com")
    _payment(db, subject, signature="SIG_fake_same")
    _payment(db, twin, signature="SIG_fake_same")
    for i in range(10):
        _attribute(db, _tenant(db), marketer, at=NOW - timedelta(hours=1, minutes=i))

    assert evaluate_flags(db, subject.id) == [
        "shared_domain",
        "shared_card",
        "shared_origin",
        "self_referral",
        "marketer_burst",
    ]


def test_no_flags_for_clean_tenant(db):
    marketer = _marketer(db)
    tenant = _tenant(db)
    _attribute(db, tenant, marketer, origin="q" * 64)
    _report(db, tenant, "clean-business.example.com")
    _payment(db, tenant, signature="SIG_fake_unique")
    assert evaluate_flags(db, tenant.id) == []


def test_unattributed_tenant_only_gets_domain_flag(db):
    a, b = _tenant(db), _tenant(db)
    _widget_host(db, a, "example.com")
    _widget_host(db, b, "example.com")
    _payment(db, a, signature="SIG_fake_same")
    _payment(db, b, signature="SIG_fake_same")
    assert evaluate_flags(db, a.id) == ["shared_domain"]


def test_unknown_tenant_returns_empty(db):
    assert evaluate_flags(db, uuid4()) == []


def test_bad_domain_rows_are_skipped_not_raised(db):
    a, b = _tenant(db), _tenant(db)
    _report(db, a, "example.com")
    _url_source(db, b, "http://[bad")
    _url_source(db, b, "http://example.com/ok")
    _widget_host(db, b, "\u0000garbage")
    assert evaluate_flags(db, a.id) == ["shared_domain"]
