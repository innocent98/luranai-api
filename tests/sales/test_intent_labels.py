"""TDD: canonical sales intent labels, trigger defaults, and normalize helpers."""

from app.sales.intent import SALES_INTENTS, TRIGGER_DEFAULTS, normalize_intent, is_trigger_intent


def test_canonical_labels():
    assert SALES_INTENTS == {
        "SUPPORT", "PRICING_INQUIRY", "ENTERPRISE_PROSPECT", "DEMO_REQUEST",
        "PURCHASE_INTENT", "OBJECTION", "CHURN_RISK",
    }


def test_defaults_are_canonical_triggers():
    assert TRIGGER_DEFAULTS == ["PRICING_INQUIRY", "DEMO_REQUEST", "PURCHASE_INTENT", "ENTERPRISE_PROSPECT"]
    assert all(i in SALES_INTENTS for i in TRIGGER_DEFAULTS)


def test_normalize_maps_legacy_and_unknown():
    assert normalize_intent("PRODUCT_DEMO_REQUEST") == "DEMO_REQUEST"
    assert normalize_intent("LEAD_QUALIFICATION") == "PURCHASE_INTENT"
    assert normalize_intent("pricing_inquiry") == "PRICING_INQUIRY"
    assert normalize_intent("garbage") == "SUPPORT"


def test_is_trigger_intent():
    assert is_trigger_intent("DEMO_REQUEST", TRIGGER_DEFAULTS) is True
    assert is_trigger_intent("SUPPORT", TRIGGER_DEFAULTS) is False
