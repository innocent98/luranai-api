"""Trial tenants must be seeded with the auditor plan caps at creation.

Regression: the two tenant-creation paths hardcode trial capabilities but
omitted max_audits_per_month / max_audit_pages_per_crawl, leaving them NULL.
NULL means "no plan limit" to the auditor, so a fresh trial escaped both caps.

This matters more since _max_pages started honouring NULL properly: a NULL page
cap now resolves to AUDITOR_MAX_PAGES_CEILING (5000) rather than quietly
falling back to the 500 default, so an unseeded trial would crawl 5000 pages.
"""

from uuid import uuid4

from app.db.models.tenant import TenantCapabilities
from app.schemas.tenant import TenantCreate
from app.services.tenant_service import TenantService


def _build_tenant_create() -> TenantCreate:
    return TenantCreate(name=f"t{uuid4().hex[:8]}", display_name="Test Tenant")


def test_create_tenant_seeds_trial_audit_caps(db_session):
    result = TenantService(db_session).create_tenant(_build_tenant_create())
    tenant_id = result.id if hasattr(result, "id") else result["id"]

    cap = (
        db_session.query(TenantCapabilities).filter(TenantCapabilities.tenant_id == tenant_id).one()
    )
    # Anchor: the existing trial knowledge cap is still 2.
    assert cap.max_knowledge_sources == 2
    # The two audit caps must be seeded (not NULL = unlimited).
    assert cap.max_audits_per_month == 2, "trial should allow 2 dashboard audits/month"
    assert cap.max_audit_pages_per_crawl == 20, "trial crawl depth should be capped at 20"
