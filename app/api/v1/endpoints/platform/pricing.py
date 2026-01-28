"""
Platform Pricing Management API

Platform admin endpoints for managing subscription plan pricing and capabilities.
"""

from typing import Annotated
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from loguru import logger

from app.api.deps import get_db, get_current_super_admin
from app.db.models.platform_admin import PlatformAdmin
from app.schemas.pricing import (
    PricingPlanUpdateRequest,
    PricingPlanResponse,
    PricingPlanListResponse,
    PricingPlanCapabilitiesResponse,
)
from app.services.plan_service import PlanService
from app.services.enhanced_audit_service import EnhancedAuditService

router = APIRouter()


@router.get(
    "",
    response_model=PricingPlanListResponse,
    summary="List all subscription plans",
    description="Get all subscription plans with pricing and capabilities (platform admin only)",
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
        )
    except Exception as e:
        logger.error(f"Error updating pricing plan {tier}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update pricing plan: {str(e)}",
        )
