"""The one definition of the subscription plan rows (prices, limits, flags, feature strings).

`scripts/seed_data.py` upserts these rows and the pricing/entitlements migration carries a frozen
copy of the same numbers. Nothing else writes plan values, so a staging re-seed cannot revert
them (2026-10-16 drift incident). The `features` strings are generated from the capability values,
so what the pricing page says is what the code enforces.

Matrix source: docs/superpowers/specs/2026-10-27-pricing-and-entitlements-design.md.
"""

from typing import Any

# Lowest to highest. The entitlements resolver and the "minimum tier" hints use this order.
TIER_ORDER: tuple[str, ...] = ("trial", "lite", "professional", "growth", "enterprise")


def _plan(**kwargs: Any) -> dict[str, Any]:
    return kwargs


# Capability values only. Marketing fields (display name, price, extras) follow in PLAN_CATALOG.
_CAPABILITIES: dict[str, dict[str, Any]] = {
    "trial": _plan(
        can_access_analytics=False,
        max_knowledge_sources=2,
        can_use_url_sources=False,
        max_knowledge_mb=10,
        max_conversations_per_month=200,
        max_team_members=1,
        max_response_tokens=450,
        max_automation_actions=2,
        max_automation_executions_per_month=100,
        can_use_ai_chat_automation=True,
        can_use_lookup_chains=False,
        can_use_sales_close=True,
        can_use_crm_notifications=True,
        can_use_screen_recording=True,
        can_export_ratings=True,
        max_whatsapp_numbers=1,
        max_flows=1,
        max_nodes_per_flow=0,
        can_use_webhooks=False,
        max_webhooks_per_flow=0,
        max_audit_pages_per_crawl=20,
        max_audits_per_month=2,
    ),
    "lite": _plan(
        can_access_analytics=True,
        max_knowledge_sources=20,
        can_use_url_sources=True,
        max_knowledge_mb=50,
        max_conversations_per_month=2000,
        max_team_members=3,
        max_response_tokens=800,
        max_automation_actions=2,
        max_automation_executions_per_month=500,
        can_use_ai_chat_automation=True,
        can_use_lookup_chains=False,
        can_use_sales_close=False,
        can_use_crm_notifications=False,
        can_use_screen_recording=False,
        can_export_ratings=False,
        max_whatsapp_numbers=1,
        max_flows=3,
        max_nodes_per_flow=20,
        can_use_webhooks=False,
        max_webhooks_per_flow=0,
        max_audit_pages_per_crawl=100,
        max_audits_per_month=5,
    ),
    "professional": _plan(
        can_access_analytics=True,
        max_knowledge_sources=50,
        can_use_url_sources=True,
        max_knowledge_mb=200,
        max_conversations_per_month=10000,
        max_team_members=10,
        max_response_tokens=1200,
        max_automation_actions=15,
        max_automation_executions_per_month=5000,
        can_use_ai_chat_automation=True,
        can_use_lookup_chains=True,
        can_use_sales_close=True,
        can_use_crm_notifications=True,
        can_use_screen_recording=True,
        can_export_ratings=True,
        max_whatsapp_numbers=2,
        max_flows=10,
        max_nodes_per_flow=50,
        can_use_webhooks=True,
        max_webhooks_per_flow=3,
        max_audit_pages_per_crawl=500,
        max_audits_per_month=15,
    ),
    "growth": _plan(
        can_access_analytics=True,
        max_knowledge_sources=100,
        can_use_url_sources=True,
        max_knowledge_mb=500,
        max_conversations_per_month=25000,
        max_team_members=20,
        max_response_tokens=1400,
        max_automation_actions=40,
        max_automation_executions_per_month=25000,
        can_use_ai_chat_automation=True,
        can_use_lookup_chains=True,
        can_use_sales_close=True,
        can_use_crm_notifications=True,
        can_use_screen_recording=True,
        can_export_ratings=True,
        max_whatsapp_numbers=5,
        max_flows=25,
        max_nodes_per_flow=100,
        can_use_webhooks=True,
        max_webhooks_per_flow=10,
        max_audit_pages_per_crawl=500,
        max_audits_per_month=30,
    ),
    "enterprise": _plan(
        can_access_analytics=True,
        max_knowledge_sources=None,
        can_use_url_sources=True,
        max_knowledge_mb=None,
        max_conversations_per_month=None,
        max_team_members=None,
        max_response_tokens=1600,
        max_automation_actions=None,
        max_automation_executions_per_month=None,
        can_use_ai_chat_automation=True,
        can_use_lookup_chains=True,
        can_use_sales_close=True,
        can_use_crm_notifications=True,
        can_use_screen_recording=True,
        can_export_ratings=True,
        max_whatsapp_numbers=None,
        max_flows=None,
        max_nodes_per_flow=None,
        can_use_webhooks=True,
        max_webhooks_per_flow=None,
        max_audit_pages_per_crawl=None,
        max_audits_per_month=None,
    ),
}

# Marketing copy that is not derived from a capability value.
_EXTRAS: dict[str, dict[str, Any]] = {
    "trial": {"support": ["AI support widget", "Email support"]},
    "lite": {"support": ["Full support"]},
    "professional": {"support": ["Full support"]},
    "growth": {"support": ["Full support"]},
    "enterprise": {
        "support": [
            "Volume-based pricing (25K-300K+)",
            "Dedicated account manager",
            "Custom integrations",
            "SLA guarantees",
            "24/7 premium support",
        ]
    },
}


def _limit(value: int | None, singular: str, plural: str | None = None, unit: str = "") -> str:
    plural = plural or singular
    if value is None:
        return f"Unlimited {plural}"
    return f"{value:,}{unit} {singular if value == 1 else plural}"


def build_feature_strings(tier: str, caps: dict[str, Any]) -> list[str]:
    """Display strings for a plan, generated from its capability values."""
    features = [
        _limit(caps["max_knowledge_sources"], "knowledge source", "knowledge sources"),
        (
            "Unlimited storage"
            if caps["max_knowledge_mb"] is None
            else f"{caps['max_knowledge_mb']}MB storage"
        ),
        (
            "Unlimited conversations/month"
            if caps["max_conversations_per_month"] is None
            else f"{caps['max_conversations_per_month']:,} conversations/month"
        ),
        _limit(caps["max_team_members"], "team member", "team members"),
        _limit(caps["max_flows"], "flow", "flows"),
        _limit(caps["max_automation_actions"], "automation action", "automation actions"),
        (
            "Unlimited automation executions/month"
            if caps["max_automation_executions_per_month"] is None
            else f"{caps['max_automation_executions_per_month']:,} automation executions/month"
        ),
    ]
    if caps["can_use_ai_chat_automation"]:
        features.append("AI chat automation")
    if caps["can_use_lookup_chains"]:
        features.append("Lookup chains")
    if caps["can_use_webhooks"]:
        per_flow = caps["max_webhooks_per_flow"]
        features.append(
            "Unlimited webhooks per flow"
            if per_flow is None
            else f"Up to {per_flow} webhooks per flow"
        )
    if caps["can_use_url_sources"]:
        features.append("URL scraping")
    if caps["can_access_analytics"]:
        features.append("Analytics dashboard")
    if caps["can_use_sales_close"]:
        features.append("Meeting booking and quotes")
    if caps["can_use_crm_notifications"]:
        features.append("Slack and CRM notifications")
    if caps["can_use_screen_recording"]:
        features.append("Screen recording")
    if caps["can_export_ratings"]:
        features.append("Ratings breakdown and CSV export")
    whatsapp = caps["max_whatsapp_numbers"]
    features.append(
        "WhatsApp (beta, on request): unlimited numbers"
        if whatsapp is None
        else f"WhatsApp (beta, on request): {whatsapp} number{'' if whatsapp == 1 else 's'}"
    )
    features.extend(_EXTRAS[tier]["support"])
    return features


_IDENTITY: dict[str, dict[str, Any]] = {
    "trial": {
        "display_name": "Trial",
        "description": "Try Luran with core AI support and starter limits.",
        "monthly_price_kobo": 0,
        "annual_price_kobo": 0,
        "sort_order": 0,
    },
    "lite": {
        "display_name": "Lite",
        "description": "For small teams getting started.",
        "monthly_price_kobo": 1_500_000,  # 15,000 naira/month
        "annual_price_kobo": 15_000_000,  # 150,000 naira/year (10 months of monthly)
        "sort_order": 1,
    },
    "professional": {
        "display_name": "Professional",
        "description": "Advanced features for growing teams",
        "monthly_price_kobo": 4_000_000,  # 40,000 naira/month
        "annual_price_kobo": 40_000_000,  # 400,000 naira/year
        "sort_order": 2,
    },
    "growth": {
        "display_name": "Growth",
        "description": "Perfect for scaling businesses with growing customer support needs",
        "monthly_price_kobo": 8_000_000,  # 80,000 naira/month
        "annual_price_kobo": 80_000_000,  # 800,000 naira/year
        "sort_order": 3,
    },
    "enterprise": {
        "display_name": "Enterprise",
        "description": "Volume-based pricing for high-scale operations (25K+ conversations)",
        "monthly_price_kobo": 0,  # Contact for pricing
        "annual_price_kobo": 0,  # Contact for pricing
        "sort_order": 4,
    },
}


def plan_capabilities(tier: str) -> dict[str, Any]:
    """Capability values of a tier (a copy)."""
    return dict(_CAPABILITIES[tier])


def build_plan_rows() -> list[dict[str, Any]]:
    """Every plan row, ready for `SubscriptionPlan(**row)`, in tier order."""
    rows = []
    for tier in TIER_ORDER:
        caps = _CAPABILITIES[tier]
        rows.append(
            {
                "tier": tier,
                **_IDENTITY[tier],
                **caps,
                "features": build_feature_strings(tier, caps),
                "is_active": True,
            }
        )
    return rows
