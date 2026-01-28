"""Platform financial reporting API endpoints."""

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.orm import Session

from app.api.deps import get_current_platform_admin, get_db
from app.utils.http_rate_limiter import platform_analytics_rate_limit
from app.services.financial_reporting_service import FinancialReportingService
from app.schemas.platform_admin import PlatformAdminResponse

router = APIRouter()


@router.get("/revenue-breakdown", status_code=status.HTTP_200_OK, dependencies=[Depends(platform_analytics_rate_limit)])
def get_revenue_breakdown(
    start_date: Optional[str] = Query(None, description="Start date (ISO format)"),
    end_date: Optional[str] = Query(None, description="End date (ISO format)"),
    db: Session = Depends(get_db),
    current_admin: PlatformAdminResponse = Depends(get_current_platform_admin),
):
    """
    Get detailed revenue breakdown by tier and billing cycle.

    Returns:
        Revenue breakdown with tier and cycle analysis
    """
    try:
        financial_service = FinancialReportingService(db)

        # Parse dates if provided
        parsed_start = datetime.fromisoformat(start_date) if start_date else None
        parsed_end = datetime.fromisoformat(end_date) if end_date else None

        breakdown = financial_service.get_revenue_breakdown(
            start_date=parsed_start,
            end_date=parsed_end,
        )

        return breakdown

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid date format: {str(e)}",
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch revenue breakdown: {str(e)}",
        )


@router.get("/payment-analytics", status_code=status.HTTP_200_OK, dependencies=[Depends(platform_analytics_rate_limit)])
def get_payment_analytics(
    days: int = Query(30, ge=1, le=365, description="Days to analyze"),
    db: Session = Depends(get_db),
    current_admin: PlatformAdminResponse = Depends(get_current_platform_admin),
):
    """
    Get payment success/failure analytics.

    Returns:
        Payment analytics with success rates and failure details
    """
    try:
        financial_service = FinancialReportingService(db)
        analytics = financial_service.get_payment_analytics(days=days)

        return analytics

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch payment analytics: {str(e)}",
        )


@router.get("/forecast", status_code=status.HTTP_200_OK, dependencies=[Depends(platform_analytics_rate_limit)])
def get_subscription_forecast(
    months: int = Query(6, ge=1, le=24, description="Months to forecast"),
    db: Session = Depends(get_db),
    current_admin: PlatformAdminResponse = Depends(get_current_platform_admin),
):
    """
    Generate subscription revenue forecast.

    Returns:
        Revenue forecast based on current trends
    """
    try:
        financial_service = FinancialReportingService(db)
        forecast = financial_service.get_subscription_forecast(months=months)

        return forecast

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to generate forecast: {str(e)}",
        )


@router.get("/refunds", status_code=status.HTTP_200_OK, dependencies=[Depends(platform_analytics_rate_limit)])
def get_refund_report(
    start_date: Optional[str] = Query(None, description="Start date (ISO format)"),
    end_date: Optional[str] = Query(None, description="End date (ISO format)"),
    db: Session = Depends(get_db),
    current_admin: PlatformAdminResponse = Depends(get_current_platform_admin),
):
    """
    Get refund tracking and analysis.

    Returns:
        Refund statistics and details
    """
    try:
        financial_service = FinancialReportingService(db)

        # Parse dates if provided
        parsed_start = datetime.fromisoformat(start_date) if start_date else None
        parsed_end = datetime.fromisoformat(end_date) if end_date else None

        report = financial_service.get_refund_report(
            start_date=parsed_start,
            end_date=parsed_end,
        )

        return report

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid date format: {str(e)}",
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch refund report: {str(e)}",
        )


@router.get("/summary", status_code=status.HTTP_200_OK, dependencies=[Depends(platform_analytics_rate_limit)])
def get_financial_summary(
    db: Session = Depends(get_db),
    current_admin: PlatformAdminResponse = Depends(get_current_platform_admin),
):
    """
    Get comprehensive financial summary.

    Returns:
        Complete financial metrics including revenue, payments, and forecast
    """
    try:
        financial_service = FinancialReportingService(db)
        summary = financial_service.get_financial_summary()

        return summary

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch financial summary: {str(e)}",
        )
