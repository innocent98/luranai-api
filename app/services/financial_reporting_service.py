"""Financial reporting and forecasting service for platform admins."""

from datetime import datetime, timedelta
from typing import Any

from loguru import logger
from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.db.models.payment import PaymentTransaction
from app.db.models.subscription import SubscriptionHistory
from app.db.models.tenant import Tenant
from app.services.plan_service import PlanService


class FinancialReportingService:
    """Service for financial analytics and forecasting."""

    def __init__(self, db: Session):
        """
        Initialize financial reporting service.

        Args:
            db: Database session
        """
        self.db = db
        self.plan_service = PlanService(db)

    @staticmethod
    def _kobo_to_naira(amount_kobo: int | float | None) -> float:
        """Convert kobo (minor unit) to naira (major unit)."""
        if amount_kobo is None:
            return 0.0
        return float(amount_kobo) / 100.0

    def get_revenue_breakdown(
        self,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
    ) -> dict[str, Any]:
        """
        Get detailed revenue breakdown by tier, billing cycle, and time period.

        Args:
            start_date: Start date for analysis (defaults to 30 days ago)
            end_date: End date for analysis (defaults to now)

        Returns:
            Dict with revenue breakdown and analysis
        """
        try:
            if not end_date:
                end_date = datetime.utcnow()
            if not start_date:
                start_date = end_date - timedelta(days=30)

            # Get successful payments in period
            payments_query = select(PaymentTransaction).where(
                and_(
                    PaymentTransaction.status == "success",
                    PaymentTransaction.created_at >= start_date,
                    PaymentTransaction.created_at <= end_date,
                )
            )

            result = self.db.execute(payments_query)
            payments = result.scalars().all()

            # Calculate total revenue (naira)
            total_revenue = self._kobo_to_naira(sum(payment.amount for payment in payments))

            # Revenue by tier (from payment snapshot to avoid drift when tenant changes plan later)
            revenue_by_tier: dict[str, float] = {}
            payment_counts_by_tier: dict[str, int] = {}

            for payment in payments:
                tier = payment.tier.lower() if payment.tier else "unknown"
                if tier not in revenue_by_tier:
                    revenue_by_tier[tier] = 0.0
                    payment_counts_by_tier[tier] = 0

                revenue_by_tier[tier] += self._kobo_to_naira(payment.amount)
                payment_counts_by_tier[tier] += 1

            # Revenue by billing cycle
            revenue_by_cycle: dict[str, float] = {}
            payment_counts_by_cycle: dict[str, int] = {}

            for payment in payments:
                cycle = payment.billing_cycle.lower() if payment.billing_cycle else "unknown"
                if cycle not in revenue_by_cycle:
                    revenue_by_cycle[cycle] = 0.0
                    payment_counts_by_cycle[cycle] = 0

                revenue_by_cycle[cycle] += self._kobo_to_naira(payment.amount)
                payment_counts_by_cycle[cycle] += 1

            # Ensure expected keys always exist for frontend rendering
            revenue_by_cycle.setdefault("monthly", 0.0)
            revenue_by_cycle.setdefault("annual", 0.0)

            # Average revenue per payment
            avg_revenue = (total_revenue / len(payments)) if payments else 0.0

            # Daily revenue breakdown
            daily_revenue: dict[str, float] = {}
            current_date = start_date.date()
            end = end_date.date()

            while current_date <= end:
                daily_revenue[current_date.isoformat()] = 0
                current_date += timedelta(days=1)

            for payment in payments:
                date_key = payment.created_at.date().isoformat()
                if date_key in daily_revenue:
                    daily_revenue[date_key] += self._kobo_to_naira(payment.amount)

            return {
                "period": {
                    "start": start_date.isoformat(),
                    "end": end_date.isoformat(),
                    "days": (end_date - start_date).days,
                },
                "total_revenue": float(total_revenue),
                "total_payments": len(payments),
                "average_revenue_per_payment": float(avg_revenue),
                "revenue_by_tier": {k: float(v) for k, v in revenue_by_tier.items()},
                "payment_counts_by_tier": payment_counts_by_tier,
                "revenue_by_billing_cycle": {k: float(v) for k, v in revenue_by_cycle.items()},
                "payment_counts_by_cycle": payment_counts_by_cycle,
                "daily_revenue": {k: float(v) for k, v in daily_revenue.items()},
                "currency": "NGN",
            }

        except Exception as e:
            logger.error(f"Error getting revenue breakdown: {e}")
            raise

    def get_payment_analytics(
        self,
        days: int = 30,
    ) -> dict[str, Any]:
        """
        Get payment success/failure analytics.

        Args:
            days: Number of days to analyze (default 30)

        Returns:
            Dict with payment analytics
        """
        try:
            end_date = datetime.utcnow()
            start_date = end_date - timedelta(days=days)

            # Get all payment attempts
            payments_query = select(PaymentTransaction).where(
                and_(
                    PaymentTransaction.created_at >= start_date,
                    PaymentTransaction.created_at <= end_date,
                )
            )

            result = self.db.execute(payments_query)
            payments = result.scalars().all()

            # Count by status
            status_counts = {"success": 0, "failed": 0, "pending": 0}
            for payment in payments:
                if payment.status in status_counts:
                    status_counts[payment.status] += 1

            total_attempts = len(payments)
            success_rate = (
                (status_counts["success"] / total_attempts * 100) if total_attempts > 0 else 0
            )

            # Revenue from successful payments
            successful_revenue = self._kobo_to_naira(
                sum(payment.amount for payment in payments if payment.status == "success")
            )

            # Potential lost revenue from failures
            failed_revenue = self._kobo_to_naira(
                sum(payment.amount for payment in payments if payment.status == "failed")
            )

            # Payment methods breakdown (if tracked)
            payment_methods: dict[str, dict[str, float]] = {}
            for payment in payments:
                method = payment.channel or "unknown"
                if method not in payment_methods:
                    payment_methods[method] = {"count": 0.0, "success": 0.0, "failed": 0.0}

                payment_methods[method]["count"] += 1.0
                if payment.status == "success":
                    payment_methods[method]["success"] += 1.0
                elif payment.status == "failed":
                    payment_methods[method]["failed"] += 1.0

            # Calculate success rates by method
            for _method, stats in payment_methods.items():
                if stats["count"] > 0:
                    stats["success_rate"] = stats["success"] / stats["count"] * 100

            # Recent failed payments for investigation
            recent_failures_query = (
                select(PaymentTransaction)
                .where(
                    and_(
                        PaymentTransaction.status == "failed",
                        PaymentTransaction.created_at >= start_date,
                    )
                )
                .order_by(PaymentTransaction.created_at.desc())
                .limit(10)
            )

            failures_result = self.db.execute(recent_failures_query)
            recent_failures = failures_result.scalars().all()

            failure_details = [
                {
                    "payment_reference": str(payment.reference),
                    "amount": self._kobo_to_naira(payment.amount),
                    "tenant_id": str(payment.tenant_id),
                    "created_at": payment.created_at.isoformat(),
                }
                for payment in recent_failures
            ]

            return {
                "period_days": days,
                "total_attempts": total_attempts,
                "status_counts": status_counts,
                "success_rate": float(success_rate),
                "successful_revenue": float(successful_revenue),
                "potential_lost_revenue": float(failed_revenue),
                "payment_methods": {
                    method: {
                        "count": stats["count"],
                        "success": stats["success"],
                        "failed": stats["failed"],
                        "success_rate": float(stats.get("success_rate", 0)),
                    }
                    for method, stats in payment_methods.items()
                },
                "recent_failures": failure_details,
                "currency": "NGN",
            }

        except Exception as e:
            logger.error(f"Error getting payment analytics: {e}")
            raise

    def get_subscription_forecast(
        self,
        months: int = 6,
    ) -> dict[str, Any]:
        """
        Generate subscription revenue forecast based on current trends.

        Args:
            months: Number of months to forecast (default 6)

        Returns:
            Dict with revenue forecast
        """
        try:
            # Get current active subscriptions
            active_query = (
                select(
                    Tenant.subscription_tier,
                    Tenant.billing_cycle,
                    func.count(Tenant.id).label("count"),
                )
                .where(
                    and_(
                        Tenant.subscription_status == "active",
                        Tenant.subscription_tier != "trial",
                    )
                )
                .group_by(Tenant.subscription_tier, Tenant.billing_cycle)
            )

            result = self.db.execute(active_query)
            active_subscriptions = result.all()

            # Get active plan pricing from database (single source of truth)
            active_plans = self.plan_service.get_all_plans(active_only=True)
            price_by_tier: dict[str, dict[str, float]] = {}
            for plan in active_plans:
                tier = str(plan.tier).lower()
                price_by_tier[tier] = {
                    "monthly": self._kobo_to_naira(plan.monthly_price_kobo),
                    "annual": self._kobo_to_naira(plan.annual_price_kobo),
                }

            # Calculate current MRR (naira)
            current_mrr: float = 0.0
            for tier, cycle, count in active_subscriptions:
                tier_lower = tier.lower()
                if tier_lower in price_by_tier:
                    if cycle == "monthly":
                        current_mrr += price_by_tier[tier_lower]["monthly"] * count
                    elif cycle == "annual":
                        current_mrr += (price_by_tier[tier_lower]["annual"] / 12) * count

            # Get growth rate from last 3 months
            three_months_ago = datetime.utcnow() - timedelta(days=90)
            two_months_ago = datetime.utcnow() - timedelta(days=60)

            # Conversions 3 months ago
            old_conversions_query = select(func.count(Tenant.id)).where(
                and_(
                    Tenant.subscription_status == "active",
                    Tenant.subscription_starts_at >= three_months_ago,
                    Tenant.subscription_starts_at < two_months_ago,
                )
            )
            old_result = self.db.execute(old_conversions_query)
            old_conversions = old_result.scalar() or 0

            # Recent conversions (last month)
            recent_month_start = datetime.utcnow() - timedelta(days=30)
            recent_conversions_query = select(func.count(Tenant.id)).where(
                and_(
                    Tenant.subscription_status == "active",
                    Tenant.subscription_starts_at >= recent_month_start,
                )
            )
            recent_result = self.db.execute(recent_conversions_query)
            recent_conversions = recent_result.scalar() or 0

            # Calculate monthly growth rate
            used_default_growth = False
            if old_conversions > 0:
                monthly_growth_rate = (recent_conversions - old_conversions) / old_conversions
            else:
                monthly_growth_rate = 0.0
                used_default_growth = True

            # Cap growth rate at reasonable limits
            monthly_growth_rate = max(
                min(monthly_growth_rate, 0.20), -0.10
            )  # Between -10% and +20%

            # Generate forecast
            forecast: list[dict[str, float | int | str]] = []
            projected_mrr = float(current_mrr)

            for month in range(1, months + 1):
                projected_mrr *= 1 + monthly_growth_rate

                forecast.append(
                    {
                        "month": month,
                        "date": (datetime.utcnow() + timedelta(days=30 * month)).strftime("%Y-%m"),
                        "projected_mrr": float(projected_mrr),
                        "projected_arr": float(projected_mrr * 12),
                    }
                )

            # Calculate total projected revenue
            total_projected = sum(float(f["projected_mrr"]) for f in forecast)

            return {
                "current_mrr": float(current_mrr),
                "current_arr": float(current_mrr * 12),
                "monthly_growth_rate": float(monthly_growth_rate * 100),
                "forecast_months": months,
                "forecast": forecast,
                "total_projected_revenue": float(total_projected),
                "currency": "NGN",
                "disclaimer": (
                    "Limited historical conversion data; growth assumed at 0% for this forecast window."
                    if used_default_growth
                    else "Forecast based on recent conversion trends and may vary with market conditions."
                ),
            }

        except Exception as e:
            logger.error(f"Error generating subscription forecast: {e}")
            raise

    def get_refund_report(
        self,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
    ) -> dict[str, Any]:
        """
        Get refund tracking and analysis.

        Args:
            start_date: Start date for analysis (defaults to 30 days ago)
            end_date: End date for analysis (defaults to now)

        Returns:
            Dict with refund statistics
        """
        try:
            if not end_date:
                end_date = datetime.utcnow()
            if not start_date:
                start_date = end_date - timedelta(days=30)

            # Query subscription history for refunds/cancellations
            refunds_query = select(SubscriptionHistory).where(
                and_(
                    SubscriptionHistory.created_at >= start_date,
                    SubscriptionHistory.created_at <= end_date,
                    SubscriptionHistory.action == "refund",
                )
            )

            result = self.db.execute(refunds_query)
            refund_records = result.scalars().all()

            # Count refunds
            total_refunds = len(refund_records)

            # Estimate refunded amount (if we had refund amounts in metadata)
            # For now, we'll track count only
            refunds_by_tier: dict[str, int] = {}
            for record in refund_records:
                tenant_query = select(Tenant).where(Tenant.id == record.tenant_id)
                tenant_result = self.db.execute(tenant_query)
                tenant = tenant_result.scalars().first()

                if tenant:
                    tier = record.previous_tier or tenant.subscription_tier
                    tier_lower = tier.lower() if tier else "unknown"

                    if tier_lower not in refunds_by_tier:
                        refunds_by_tier[tier_lower] = 0

                    refunds_by_tier[tier_lower] += 1

            # Refunds by reason (if tracked in metadata)
            refunds_by_reason = {}
            for record in refund_records:
                reason = record.reason or "No reason provided"
                if reason not in refunds_by_reason:
                    refunds_by_reason[reason] = 0
                refunds_by_reason[reason] += 1

            # Recent refunds
            recent_refunds = [
                {
                    "tenant_id": str(record.tenant_id),
                    "tier": record.previous_tier,
                    "reason": record.reason,
                    "created_at": record.created_at.isoformat(),
                }
                for record in sorted(refund_records, key=lambda x: x.created_at, reverse=True)[:10]
            ]

            return {
                "period": {
                    "start": start_date.isoformat(),
                    "end": end_date.isoformat(),
                },
                "total_refunds": total_refunds,
                "refunds_by_tier": refunds_by_tier,
                "refunds_by_reason": refunds_by_reason,
                "recent_refunds": recent_refunds,
            }

        except Exception as e:
            logger.error(f"Error getting refund report: {e}")
            raise

    def get_financial_summary(self) -> dict[str, Any]:
        """
        Get comprehensive financial summary.

        Returns:
            Dict with all financial metrics
        """
        try:
            revenue_breakdown = self.get_revenue_breakdown()
            payment_analytics = self.get_payment_analytics()
            forecast = self.get_subscription_forecast()
            refunds = self.get_refund_report()

            return {
                "revenue": revenue_breakdown,
                "payments": payment_analytics,
                "forecast": forecast,
                "refunds": refunds,
                "generated_at": datetime.utcnow().isoformat(),
            }

        except Exception as e:
            logger.error(f"Error generating financial summary: {e}")
            raise
