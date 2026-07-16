import uuid

from app.db.models.subscription_plan import SubscriptionPlan
from app.db.models.tenant import TenantCapabilities


def test_capability_columns_exist(db):
    cap = TenantCapabilities(
        tenant_id=uuid.uuid4(),
        max_audit_pages_per_crawl=200,
        max_audits_per_month=5,
    )
    db.add(cap)
    db.flush()
    assert cap.max_audit_pages_per_crawl == 200
    assert cap.max_audits_per_month == 5


def test_plan_columns_exist_and_serialize(db):
    plan = SubscriptionPlan(
        tier="unittest_tier",
        display_name="Unit",
        description="x",
        monthly_price_kobo=0,
        annual_price_kobo=0,
        max_response_tokens=500,
        max_audit_pages_per_crawl=500,
        max_audits_per_month=15,
    )
    db.add(plan)
    db.flush()
    assert plan.get_capabilities()["max_audit_pages_per_crawl"] == 500
    assert plan.get_capabilities()["max_audits_per_month"] == 15


# ---------------------------------------------------------------------------
# _max_pages: how a plan's crawl budget is resolved.
#
# NULL means "no plan limit" for every other capability (max_audits_per_month,
# max_knowledge_sources both read it that way), but _max_pages used to test the
# value for truthiness and fall through to the 500 default — so Enterprise,
# whose caps are NULL, silently crawled no deeper than Growth.
# ---------------------------------------------------------------------------


def _report(tenant_id, scope):
    from app.db.models.audit import AuditReport

    return AuditReport(tenant_id=tenant_id, url="https://x.com/", domain="x.com", scope=scope)


def test_public_scope_ignores_tenant_caps(db):
    from app.auditor.pipeline import _max_pages
    from app.core.config import settings
    from app.db.models.audit import AuditScope

    assert _max_pages(db, _report(None, AuditScope.PUBLIC)) == settings.AUDITOR_PUBLIC_MAX_PAGES


def test_null_cap_is_unlimited_not_the_default(db):
    """Enterprise: NULL = no plan limit, bounded only by the safety ceiling."""
    import uuid

    from app.auditor.pipeline import _max_pages
    from app.core.config import settings
    from app.db.models.audit import AuditScope
    from app.db.models.tenant import TenantCapabilities

    tid = uuid.uuid4()
    db.add(TenantCapabilities(tenant_id=tid, max_audit_pages_per_crawl=None))
    db.flush()
    resolved = _max_pages(db, _report(tid, AuditScope.DASHBOARD))
    assert resolved == settings.AUDITOR_MAX_PAGES_CEILING
    assert resolved > settings.AUDITOR_DASHBOARD_DEFAULT_MAX_PAGES


def test_configured_cap_is_honoured(db):
    import uuid

    from app.auditor.pipeline import _max_pages
    from app.db.models.audit import AuditScope
    from app.db.models.tenant import TenantCapabilities

    tid = uuid.uuid4()
    db.add(TenantCapabilities(tenant_id=tid, max_audit_pages_per_crawl=20))
    db.flush()
    assert _max_pages(db, _report(tid, AuditScope.DASHBOARD)) == 20


def test_zero_cap_falls_back_rather_than_emptying_the_crawl(db):
    """A 0 cap would truncate seeds to [], which Phase 2b reads as 'the guard
    refused the root' and reports UNREACHABLE — a wrong diagnosis for what is
    really a misconfigured plan."""
    import uuid

    from app.auditor.pipeline import _max_pages
    from app.core.config import settings
    from app.db.models.audit import AuditScope
    from app.db.models.tenant import TenantCapabilities

    tid = uuid.uuid4()
    db.add(TenantCapabilities(tenant_id=tid, max_audit_pages_per_crawl=0))
    db.flush()
    assert _max_pages(db, _report(tid, AuditScope.DASHBOARD)) == (
        settings.AUDITOR_DASHBOARD_DEFAULT_MAX_PAGES
    )


def test_missing_capabilities_row_uses_default(db):
    import uuid

    from app.auditor.pipeline import _max_pages
    from app.core.config import settings
    from app.db.models.audit import AuditScope

    assert _max_pages(db, _report(uuid.uuid4(), AuditScope.DASHBOARD)) == (
        settings.AUDITOR_DASHBOARD_DEFAULT_MAX_PAGES
    )
