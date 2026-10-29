"""Enterprise volume-based pricing calculator."""


class EnterprisePricingCalculator:
    """Calculate Enterprise tier pricing based on volume."""

    # Volume tiers: (min_conversations, max_conversations, price_per_conversation_kobo)
    VOLUME_TIERS: list[tuple[int, float, int]] = [
        (25_000, 50_000, 300),  # ₦3.00 per conversation
        (50_001, 100_000, 275),  # ₦2.75 per conversation
        (100_001, 200_000, 250),  # ₦2.50 per conversation
        (200_001, 300_000, 230),  # ₦2.30 per conversation
        (300_001, float("inf"), 220),  # ₦2.20 per conversation (capped)
    ]

    MIN_ENTERPRISE_CONVERSATIONS = 25_000

    @classmethod
    def calculate_monthly_cost(cls, conversations: int) -> dict:
        """
        Calculate monthly cost for Enterprise tier based on conversation volume.

        Args:
            conversations: Expected monthly conversation volume

        Returns:
            Dict with pricing breakdown:
            {
                'conversations': int,
                'tier_breakdown': [
                    {
                        'range': '25,000-50,000',
                        'conversations_in_tier': 25000,
                        'price_per_conv_kobo': 300,
                        'price_per_conv_naira': 3.00,
                        'subtotal_kobo': 7500000,
                        'subtotal_naira': 75000
                    },
                    ...
                ],
                'total_cost_kobo': int,
                'total_cost_naira': float,
                'average_price_per_conv_kobo': int,
                'average_price_per_conv_naira': float,
                'margin_percentage': float  # Profit margin at ₦0.56 AI cost
            }
        """
        if conversations < cls.MIN_ENTERPRISE_CONVERSATIONS:
            return {
                "error": "minimum_volume_not_met",
                "message": "Enterprise pricing starts at 25,000 conversations/month",
                # Growth covers up to its 10,000 limit; between that and 25,000 nothing standard fits.
                "recommended_tier": "growth" if conversations <= 10_000 else "custom",
                "growth_tier_cost_kobo": 8_000_000,
                "growth_tier_cost_naira": 80_000,
            }

        # Find which tier the total volume falls into
        applicable_tier = None
        for min_vol, max_vol, price_per_conv in cls.VOLUME_TIERS:
            if min_vol <= conversations <= max_vol or (
                max_vol == float("inf") and conversations >= min_vol
            ):
                applicable_tier = (min_vol, max_vol, price_per_conv)
                break

        if not applicable_tier:
            return {
                "error": "volume_out_of_range",
                "message": "Volume does not fall within any pricing tier",
            }

        min_vol, max_vol, price_per_conv = applicable_tier

        # Calculate total cost - ALL conversations at this tier's rate
        total_cost_kobo = conversations * price_per_conv

        # Format range display
        range_display = (
            f"{min_vol:,}-{int(max_vol):,}" if max_vol != float("inf") else f"{min_vol:,}+"
        )

        tier_breakdown = [
            {
                "range": range_display,
                "conversations_in_tier": conversations,
                "price_per_conv_kobo": price_per_conv,
                "price_per_conv_naira": round(price_per_conv / 100, 2),
                "subtotal_kobo": total_cost_kobo,
                "subtotal_naira": round(total_cost_kobo / 100, 2),
            }
        ]

        # Calculate averages
        total_cost_naira = round(total_cost_kobo / 100, 2)
        avg_price_per_conv_kobo = round(total_cost_kobo / conversations)
        avg_price_per_conv_naira = round(avg_price_per_conv_kobo / 100, 2)

        # Calculate profit margin (assuming ₦0.56 AI cost per conversation)
        ai_cost_kobo = 56  # ₦0.56 per conversation
        total_ai_cost_kobo = conversations * ai_cost_kobo
        profit_kobo = total_cost_kobo - total_ai_cost_kobo
        margin_percentage = (
            round((profit_kobo / total_cost_kobo) * 100, 2) if total_cost_kobo > 0 else 0
        )

        return {
            "conversations": conversations,
            "tier_breakdown": tier_breakdown,
            "total_cost_kobo": total_cost_kobo,
            "total_cost_naira": total_cost_naira,
            "average_price_per_conv_kobo": avg_price_per_conv_kobo,
            "average_price_per_conv_naira": avg_price_per_conv_naira,
            "ai_cost_estimate": {
                "cost_per_conv_kobo": ai_cost_kobo,
                "total_kobo": total_ai_cost_kobo,
                "total_naira": round(total_ai_cost_kobo / 100, 2),
            },
            "profit_margin": {
                "profit_kobo": profit_kobo,
                "profit_naira": round(profit_kobo / 100, 2),
                "margin_percentage": margin_percentage,
            },
        }

    @classmethod
    def get_pricing_tiers(cls) -> list[dict]:
        """Get all volume pricing tiers for display."""
        tiers = []
        for min_vol, max_vol, price_per_conv in cls.VOLUME_TIERS:
            range_display = (
                f"{min_vol:,}-{int(max_vol):,}" if max_vol != float("inf") else f"{min_vol:,}+"
            )

            tiers.append(
                {
                    "range": range_display,
                    "min_conversations": min_vol,
                    "max_conversations": int(max_vol) if max_vol != float("inf") else None,
                    "price_per_conv_kobo": price_per_conv,
                    "price_per_conv_naira": round(price_per_conv / 100, 2),
                    "example_monthly_cost_naira": round((min_vol * price_per_conv) / 100, 2),
                }
            )

        return tiers

    @classmethod
    def compare_with_fixed_tiers(cls, conversations: int) -> dict:
        """
        Compare Enterprise pricing with fixed Growth tier.

        Args:
            conversations: Monthly conversation volume

        Returns:
            Comparison showing which option is better
        """
        # Growth tier: ₦80,000 for 10,000 conversations (its plan limit, plan_catalog.py).
        growth_price_kobo = 8_000_000
        growth_max_conversations = 10_000

        growth_tier = {
            "cost_kobo": growth_price_kobo,
            "cost_naira": 80_000,
            "conversations_included": growth_max_conversations,
            "price_per_conv_naira": round(growth_price_kobo / growth_max_conversations / 100, 2),
        }

        if conversations <= growth_max_conversations:
            return {
                "recommended_tier": "growth",
                "reason": f"Volume ({conversations:,}) within Growth tier limit",
                "conversations": conversations,
                "growth_tier": growth_tier,
                "note": f"Growth tier is recommended for volumes up to {growth_max_conversations:,} conversations",
                "recommendation": "Growth tier covers this volume",
            }

        if conversations < cls.MIN_ENTERPRISE_CONVERSATIONS:
            # Above Growth's cap, below where volume pricing starts: no standard plan fits.
            return {
                "recommended_tier": "custom",
                "reason": (
                    f"Volume ({conversations:,}) is above the Growth limit "
                    f"({growth_max_conversations:,}) and below the Enterprise minimum "
                    f"({cls.MIN_ENTERPRISE_CONVERSATIONS:,})"
                ),
                "conversations": conversations,
                "growth_tier": growth_tier,
                "note": "No standard plan covers this volume; agree custom pricing with the customer",
                "recommendation": "Quote custom pricing for this volume",
            }

        enterprise_pricing = cls.calculate_monthly_cost(conversations)

        # Calculate what Growth tier would cost if they went over
        overage = conversations - growth_max_conversations
        # Assuming overage charged at Professional rate: ₦4.00/conv
        overage_cost_kobo = overage * 400  # ₦4.00
        growth_with_overage_kobo = growth_price_kobo + overage_cost_kobo

        savings_kobo = growth_with_overage_kobo - enterprise_pricing["total_cost_kobo"]
        savings_naira = round(savings_kobo / 100, 2)

        return {
            "recommended_tier": "enterprise",
            "conversations": conversations,
            "growth_tier_with_overage": {
                "base_cost_kobo": growth_price_kobo,
                "base_cost_naira": 80_000,
                "overage_conversations": overage,
                "overage_cost_kobo": overage_cost_kobo,
                "overage_cost_naira": round(overage_cost_kobo / 100, 2),
                "total_cost_kobo": growth_with_overage_kobo,
                "total_cost_naira": round(growth_with_overage_kobo / 100, 2),
            },
            "enterprise_tier": {
                "total_cost_kobo": enterprise_pricing["total_cost_kobo"],
                "total_cost_naira": enterprise_pricing["total_cost_naira"],
                "average_price_per_conv_naira": enterprise_pricing["average_price_per_conv_naira"],
            },
            "savings": {
                "amount_kobo": savings_kobo,
                "amount_naira": savings_naira,
                "percentage": (
                    round((savings_kobo / growth_with_overage_kobo) * 100, 2)
                    if growth_with_overage_kobo > 0
                    else 0
                ),
            },
            "recommendation": "Enterprise tier is more cost-effective for this volume",
        }


def calculate_enterprise_cost(conversations: int) -> dict:
    """Quick wrapper for enterprise pricing calculation."""
    return EnterprisePricingCalculator.calculate_monthly_cost(conversations)
