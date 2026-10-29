"""Growth vs Enterprise comparison after Growth's limit dropped to 10,000 conversations/month."""

import pytest

from app.schemas.pricing import TierComparison
from app.services.plan_catalog import plan_capabilities
from app.utils.enterprise_pricing import EnterprisePricingCalculator as Calculator

GROWTH_LIMIT = 10_000


def test_the_growth_limit_used_here_is_the_plan_limit():
    assert plan_capabilities("growth")["max_conversations_per_month"] == GROWTH_LIMIT


@pytest.mark.parametrize("volume", [1, 5_000, GROWTH_LIMIT])
def test_up_to_the_growth_limit_growth_is_recommended(volume):
    result = Calculator.compare_with_fixed_tiers(volume)
    assert result["recommended_tier"] == "growth"
    assert result["growth_tier"]["conversations_included"] == GROWTH_LIMIT
    assert result["growth_tier"]["price_per_conv_naira"] == 8.0
    TierComparison(**result)  # the endpoint's response model accepts it


@pytest.mark.parametrize("volume", [GROWTH_LIMIT + 1, 15_000, 24_999])
def test_between_growth_and_the_enterprise_minimum_no_standard_plan_fits(volume):
    result = Calculator.compare_with_fixed_tiers(volume)
    assert result["recommended_tier"] == "custom"
    assert "10,000" in result["reason"] and "25,000" in result["reason"]
    TierComparison(**result)


def test_at_the_enterprise_minimum_enterprise_is_compared():
    result = Calculator.compare_with_fixed_tiers(25_000)
    assert result["recommended_tier"] == "enterprise"
    assert result["growth_tier_with_overage"]["overage_conversations"] == 15_000
    TierComparison(**result)


def test_the_calculator_does_not_call_growth_enough_between_the_limits():
    below = Calculator.calculate_monthly_cost(10_000)
    gap = Calculator.calculate_monthly_cost(15_000)
    assert below["error"] == "minimum_volume_not_met" and below["recommended_tier"] == "growth"
    assert gap["error"] == "minimum_volume_not_met" and gap["recommended_tier"] == "custom"
