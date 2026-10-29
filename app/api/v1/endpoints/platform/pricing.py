"""
Platform Pricing Management API

Platform admin endpoints for managing subscription plan pricing and capabilities.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from loguru import logger
from sqlalchemy.orm import Session

from app.api.deps import get_current_super_admin, get_db
from app.db.models.platform_admin import PlatformAdmin
from app.schemas.pricing import (
    EnterprisePricingResponse,
    EnterprisePricingTiersResponse,
    PricingCalculatorRequest,
    PricingPlanCapabilitiesResponse,
    PricingPlanListResponse,
    PricingPlanResponse,
    PricingPlanUpdateRequest,
    TierComparison,
    VolumePricingTier,
)
from app.services.enhanced_audit_service import EnhancedAuditService
from app.services.plan_service import PlanService
from app.utils.enterprise_pricing import EnterprisePricingCalculator
from app.utils.http_rate_limiter import platform_admin_rate_limit

router = APIRouter()


@router.get(
    "",
    response_model=PricingPlanListResponse,
    summary="List all subscription plans",
    description="Get all subscription plans with pricing and capabilities (platform admin only)",
    dependencies=[Depends(platform_admin_rate_limit)],
)
async def list_pricing_plans(
    db: Annotated[Session, Depends(get_db)],
    admin: Annotated[PlatformAdmin, Depends(get_current_super_admin)],
    include_inactive: bool = False,
) -> PricingPlanListResponse:
    """
    List all subscription plans.

    Args:
        db: Database session
        admin: Authenticated platform admin
        include_inactive: Include inactive plans

    Returns:
        List of all subscription plans with pricing and capabilities
    """
    plan_service = PlanService(db)

    # Get plans
    plans = plan_service.get_all_plans(active_only=not include_inactive)

    # Convert to response format
    plan_responses = []
    for plan in plans:
        plan_dict = plan.to_dict()
        plan_responses.append(
            PricingPlanResponse(
                tier=plan_dict["tier"],
                display_name=plan_dict["display_name"],
                description=plan_dict["description"],
                monthly_price_kobo=plan_dict["monthly_price_kobo"],
                annual_price_kobo=plan_dict["annual_price_kobo"],
                monthly_price_naira=plan_dict["monthly_price_naira"],
                annual_price_naira=plan_dict["annual_price_naira"],
                capabilities=PricingPlanCapabilitiesResponse(**plan_dict["capabilities"]),
                features=plan_dict["features"],
                is_active=plan_dict["is_active"],
                sort_order=plan_dict["sort_order"],
            )
        )

    logger.info(f"Platform admin {admin.email} listed {len(plan_responses)} pricing plans")

    return PricingPlanListResponse(
        plans=plan_responses,
        total=len(plan_responses),
    )


@router.put(
    "/{tier}",
    response_model=PricingPlanResponse,
    summary="Update subscription plan",
    description="Update pricing, capabilities, or metadata for a subscription plan (platform admin only)",
    dependencies=[Depends(platform_admin_rate_limit)],
)
async def update_pricing_plan(
    tier: str,
    update_data: PricingPlanUpdateRequest,
    db: Annotated[Session, Depends(get_db)],
    admin: Annotated[PlatformAdmin, Depends(get_current_super_admin)],
) -> PricingPlanResponse:
    """
    Update subscription plan pricing and capabilities.

    Args:
        tier: Plan tier to update
        update_data: Update data with pricing, capabilities, and reason
        db: Database session
        admin: Authenticated platform admin

    Returns:
        Updated subscription plan

    Raises:
        404: If plan not found
        400: If validation fails
    """
    plan_service = PlanService(db)
    audit_service = EnhancedAuditService(db)

    try:
        # Get current plan state for audit
        current_plan = plan_service.get_plan(tier)
        previous_state = current_plan.to_dict()

        # Convert capabilities update to dict
        capabilities_dict = None
        if update_data.capabilities:
            capabilities_dict = update_data.capabilities.model_dump(exclude_none=True)

        # Update plan
        updated_plan = plan_service.update_plan(
            tier=tier,
            monthly_price_kobo=update_data.monthly_price_kobo,
            annual_price_kobo=update_data.annual_price_kobo,
            display_name=update_data.display_name,
            description=update_data.description,
            capabilities=capabilities_dict,
            features=update_data.features,
            is_active=update_data.is_active,
            sort_order=update_data.sort_order,
        )

        # Create audit log
        audit_service.log_action(
            action_type="update_pricing_plan",
            performed_by_type="platform_admin",
            performed_by_id=admin.id,
            performed_by_name=admin.email,
            target_type="subscription_plan",
            target_id=updated_plan.id,
            description=f"Updated pricing plan: {tier}",
            severity="high",
            metadata={
                "tier": tier,
                "reason": update_data.reason,
                "previous_state": {
                    "monthly_price_kobo": previous_state["monthly_price_kobo"],
                    "annual_price_kobo": previous_state["annual_price_kobo"],
                    "capabilities": previous_state["capabilities"],
                    "features": previous_state["features"],
                    "is_active": previous_state["is_active"],
                },
                "new_state": {
                    "monthly_price_kobo": updated_plan.monthly_price_kobo,
                    "annual_price_kobo": updated_plan.annual_price_kobo,
                    "capabilities": updated_plan.get_capabilities(),
                    "features": updated_plan.features,
                    "is_active": updated_plan.is_active,
                },
            },
        )

        logger.info(
            f"Platform admin {admin.email} updated pricing plan {tier}: {update_data.reason}"
        )

        # Convert to response
        plan_dict = updated_plan.to_dict()
        return PricingPlanResponse(
            tier=plan_dict["tier"],
            display_name=plan_dict["display_name"],
            description=plan_dict["description"],
            monthly_price_kobo=plan_dict["monthly_price_kobo"],
            annual_price_kobo=plan_dict["annual_price_kobo"],
            monthly_price_naira=plan_dict["monthly_price_naira"],
            annual_price_naira=plan_dict["annual_price_naira"],
            capabilities=PricingPlanCapabilitiesResponse(**plan_dict["capabilities"]),
            features=plan_dict["features"],
            is_active=plan_dict["is_active"],
            sort_order=plan_dict["sort_order"],
        )

    except ValueError as e:
        logger.warning(f"Validation error updating plan {tier}: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        ) from e
    except Exception as e:
        logger.error(f"Error updating pricing plan {tier}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update pricing plan: {str(e)}",
        ) from e


@router.get(
    "/enterprise/tiers",
    response_model=EnterprisePricingTiersResponse,
    summary="Get Enterprise Volume Pricing Tiers",
    description="Get all Enterprise volume-based pricing tiers (platform admin only)",
    dependencies=[Depends(platform_admin_rate_limit)],
)
async def get_enterprise_pricing_tiers(
    admin: Annotated[PlatformAdmin, Depends(get_current_super_admin)],
) -> EnterprisePricingTiersResponse:
    """
    Get all Enterprise volume-based pricing tiers.

    Returns the pricing structure for Enterprise tier based on conversation volume.
    Useful for quoting customers and sales proposals.

    **Volume Tiers:**
    - 25,000-50,000: ₦3.00/conversation
    - 50,001-100,000: ₦2.75/conversation
    - 100,001-200,000: ₦2.50/conversation
    - 200,001-300,000: ₦2.30/conversation
    - 300,001+: ₦2.20/conversation (capped)
    """
    tiers = EnterprisePricingCalculator.get_pricing_tiers()
    tier_models = [VolumePricingTier.model_validate(tier) for tier in tiers]

    logger.info(f"Platform admin {admin.email} retrieved Enterprise pricing tiers")

    return EnterprisePricingTiersResponse(
        tiers=tier_models,
        note=(
            "Enterprise pricing is volume-based with tiered rates. "
            "Higher volumes get progressively lower per-conversation rates, "
            "capped at ₦2.20/conversation for 300K+ conversations."
        ),
    )


@router.post(
    "/enterprise/calculate",
    response_model=EnterprisePricingResponse,
    summary="Calculate Enterprise Pricing",
    description="Calculate monthly cost for Enterprise tier based on conversation volume (platform admin only)",
    dependencies=[Depends(platform_admin_rate_limit)],
)
async def calculate_enterprise_pricing(
    request: PricingCalculatorRequest,
    admin: Annotated[PlatformAdmin, Depends(get_current_super_admin)],
) -> EnterprisePricingResponse:
    """
    Calculate monthly cost for Enterprise tier based on conversation volume.

    Platform admins can use this to quote Enterprise customers based on their
    expected conversation volume. Includes cost breakdown, profit margins, and
    AI cost estimates.

    **Example:**
    - 50,000 conversations/month = ₦142,500 (₦2.85/conv average)
    - 150,000 conversations/month = ₦390,000 (₦2.60/conv average)
    - 350,000 conversations/month = ₦770,000 (₦2.20/conv average - capped)

    **Raises:**
    - 400: If conversation volume is below 25,000 (minimum for Enterprise)
    """
    result = EnterprisePricingCalculator.calculate_monthly_cost(request.conversations)

    if "error" in result:
        logger.warning(
            f"Platform admin {admin.email} attempted Enterprise calculation for {request.conversations} conversations (below minimum)"
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": result["error"],
                "message": result["message"],
                "recommended_tier": result.get("recommended_tier"),
                "growth_tier_cost_naira": result.get("growth_tier_cost_naira"),
            },
        )

    logger.info(
        f"Platform admin {admin.email} calculated Enterprise pricing for {request.conversations:,} conversations: ₦{result['total_cost_naira']:,.2f}"
    )

    return EnterprisePricingResponse(**result)


@router.post(
    "/enterprise/compare",
    response_model=TierComparison,
    summary="Compare Growth vs Enterprise Pricing",
    description="Compare Growth tier pricing vs Enterprise volume-based pricing (platform admin only)",
    dependencies=[Depends(platform_admin_rate_limit)],
)
async def compare_growth_vs_enterprise(
    request: PricingCalculatorRequest,
    admin: Annotated[PlatformAdmin, Depends(get_current_super_admin)],
) -> TierComparison:
    """
    Compare Growth tier pricing vs Enterprise volume-based pricing.

    Helps determine which tier is more cost-effective for a given volume.
    Useful for sales conversations and upgrade recommendations.

    **Logic:**
    - ≤10,000 conversations: Growth tier recommended (fixed ₦80K, its plan limit)
    - 10,001-24,999 conversations: no standard plan fits; custom pricing is recommended
    - ≥25,000 conversations: Compares Growth + overage vs Enterprise volume pricing

    **Example:**
    - 40,000 conversations:
      - Growth + overage: ₦80K + (15K × ₦4) = ₦140K
      - Enterprise: ₦120K (saves ₦20K)
    """
    result = EnterprisePricingCalculator.compare_with_fixed_tiers(request.conversations)

    logger.info(
        f"Platform admin {admin.email} compared Growth vs Enterprise for {request.conversations:,} conversations: {result['recommended_tier']} recommended"
    )

    return TierComparison(**result)
