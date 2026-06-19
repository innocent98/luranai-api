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
