"""Platform-wide analytics service for admin dashboards."""

from datetime import datetime, timedelta
from typing import Any

from loguru import logger
from sqlalchemy import and_, distinct, func, or_, select
from sqlalchemy.orm import Session

from app.db.models.conversation import ConversationSession, Ticket
from app.db.models.knowledge import KnowledgeSource
from app.db.models.subscription import SubscriptionHistory
from app.db.models.tenant import Tenant
from app.services.visit_analytics_service import VisitAnalyticsService


class PlatformAnalyticsService:
    """Service for platform-wide analytics and reporting."""

    def __init__(self, db: Session):
        """
        Initialize platform analytics service.

        Args:
            db: Database session
        """
        self.db = db

    def get_revenue_metrics(
        self,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
    ) -> dict[str, Any]:
        """
        Get revenue metrics including MRR, ARR, and growth.

        Args:
            start_date: Start date for analysis (defaults to 30 days ago)
            end_date: End date for analysis (defaults to now)

        Returns:
            Dict with MRR, ARR, growth rate, and breakdown
        """
        try:
            if not end_date:
                end_date = datetime.utcnow()
            if not start_date:
                start_date = end_date - timedelta(days=30)

            # Calculate MRR (Monthly Recurring Revenue)
            # Sum of monthly subscriptions + (annual subscriptions / 12)
            active_subscriptions_query = (
                select(
                    Tenant.subscription_tier,
                    Tenant.billing_cycle,
                    func.count(Tenant.id).label("count"),
                )
                .where(
                    and_(
                        Tenant.subscription_status == "active", Tenant.subscription_tier != "trial"
                    )
                )
                .group_by(Tenant.subscription_tier, Tenant.billing_cycle)
            )

            result = self.db.execute(active_subscriptions_query)
            subscriptions = result.all()

            # Pricing tiers (in Naira)
            pricing = {
                "lite": {"monthly": 12000, "annual": 120000},
                "professional": {"monthly": 35000, "annual": 350000},
                "enterprise": {"monthly": 0, "annual": 0},  # Contact pricing
            }

            mrr = 0
            for tier, cycle, count in subscriptions:
                tier_lower = tier.lower()
                if tier_lower in pricing:
                    if cycle == "monthly":
                        mrr += pricing[tier_lower]["monthly"] * count
                    elif cycle == "annual":
                        # Annualize to monthly
                        mrr += (pricing[tier_lower]["annual"] / 12) * count

            arr = mrr * 12

            # Calculate previous period MRR for growth rate
            # Get revenue in previous period (simplified - using current active subscriptions as proxy)
            previous_mrr = mrr * 0.85  # Placeholder - should query historical data

            # Calculate growth rate
            if previous_mrr > 0:
                mrr_growth_rate = ((mrr - previous_mrr) / previous_mrr) * 100
            else:
                mrr_growth_rate = 100 if mrr > 0 else 0

            # Revenue breakdown by tier
            revenue_by_tier: dict[str, float] = {}
            for tier, cycle, count in subscriptions:
                tier_lower = tier.lower()
                if tier_lower in pricing:
                    if cycle == "monthly":
                        revenue = pricing[tier_lower]["monthly"] * count
                    elif cycle == "annual":
                        revenue = (pricing[tier_lower]["annual"] / 12) * count
                    else:
                        revenue = 0

                    if tier_lower in revenue_by_tier:
                        revenue_by_tier[tier_lower] += revenue
                    else:
                        revenue_by_tier[tier_lower] = revenue

            return {
                "mrr": float(mrr),
                "arr": float(arr),
                "mrr_growth_rate": float(mrr_growth_rate),
                "revenue_by_tier": {k: float(v) for k, v in revenue_by_tier.items()},
                "currency": "NGN",
                "period": {
                    "start": start_date.isoformat(),
                    "end": end_date.isoformat(),
                },
            }

        except Exception as e:
            logger.error(f"Error calculating revenue metrics: {e}")
            raise

    def get_subscription_metrics(
        self,
        days: int = 30,
    ) -> dict[str, Any]:
        """
        Get subscription health metrics including churn, conversion, and counts.

        Args:
            days: Number of days to analyze (default 30)

        Returns:
            Dict with subscription metrics
        """
        try:
            end_date = datetime.utcnow()
            start_date = end_date - timedelta(days=days)

            # Total active subscriptions
            active_count_query = select(func.count(Tenant.id)).where(
                Tenant.subscription_status == "active"
            )
            active_result = self.db.execute(active_count_query)
            active_count = active_result.scalar() or 0

            # Total trialing tenants
            trial_count_query = select(func.count(Tenant.id)).where(
                Tenant.subscription_status == "trialing"
            )
            trial_result = self.db.execute(trial_count_query)
            trial_count = trial_result.scalar() or 0

            # Canceled/expired subscriptions
            inactive_count_query = select(func.count(Tenant.id)).where(
                or_(
                    Tenant.subscription_status == "canceled",
                    Tenant.subscription_status == "trial_expired",
                )
            )
            inactive_result = self.db.execute(inactive_count_query)
            inactive_count = inactive_result.scalar() or 0

            # Trial conversion rate
            # Count tenants that converted from trial to paid in the period
            converted_query = select(func.count(Tenant.id)).where(
                and_(
                    Tenant.subscription_status == "active",
                    Tenant.subscription_starts_at >= start_date,
                    Tenant.subscription_starts_at <= end_date,
                )
            )
            converted_result = self.db.execute(converted_query)
            converted_count = converted_result.scalar() or 0

            # Trials that expired in period
            expired_trials_query = select(func.count(Tenant.id)).where(
                and_(
                    Tenant.subscription_status == "trial_expired",
                    Tenant.trial_ends_at >= start_date,
                    Tenant.trial_ends_at <= end_date,
                )
            )
            expired_result = self.db.execute(expired_trials_query)
            expired_trials = expired_result.scalar() or 0

            total_trial_outcomes = converted_count + expired_trials
            trial_conversion_rate = (
                (converted_count / total_trial_outcomes * 100) if total_trial_outcomes > 0 else 0
            )

            # Churn rate
            # Canceled subscriptions in period / total active at start of period
            churned_query = select(func.count(Tenant.id)).where(
                and_(
                    or_(
                        Tenant.subscription_status == "canceled",
                        Tenant.subscription_status == "past_due",
                    ),
                    Tenant.subscription_ends_at >= start_date,
                    Tenant.subscription_ends_at <= end_date,
                )
            )
            churned_result = self.db.execute(churned_query)
            churned_count = churned_result.scalar() or 0

            churn_rate = (
                (churned_count / (active_count + churned_count) * 100)
                if (active_count + churned_count) > 0
                else 0
            )

            # Subscription tier distribution
            tier_distribution_query = (
                select(Tenant.subscription_tier, func.count(Tenant.id).label("count"))
                .where(Tenant.subscription_status == "active")
                .group_by(Tenant.subscription_tier)
            )

            tier_result = self.db.execute(tier_distribution_query)
            tier_distribution: dict[str, int] = {
                str(tier): int(count) for tier, count in tier_result.all()
            }

            return {
                "active_subscriptions": active_count,
                "trialing_tenants": trial_count,
                "inactive_tenants": inactive_count,
                "trial_conversion_rate": float(trial_conversion_rate),
                "churn_rate": float(churn_rate),
                "churned_count": churned_count,
                "converted_count": converted_count,
                "tier_distribution": tier_distribution,
                "period_days": days,
            }

        except Exception as e:
            logger.error(f"Error calculating subscription metrics: {e}")
            raise

    def get_growth_metrics(
        self,
        days: int = 30,
    ) -> dict[str, Any]:
        """
        Get growth metrics including signups, upgrades, and trends.

        Args:
            days: Number of days to analyze (default 30)

        Returns:
            Dict with growth metrics and trends
        """
        try:
            end_date = datetime.utcnow()
            start_date = end_date - timedelta(days=days)

            # New signups (new tenants created)
            signups_query = select(func.count(Tenant.id)).where(
                and_(
                    Tenant.created_at >= start_date,
                    Tenant.created_at <= end_date,
                )
            )
            signups_result = self.db.execute(signups_query)
            new_signups = signups_result.scalar() or 0

            # Upgrades (trial to paid conversions)
            upgrades_query = select(func.count(Tenant.id)).where(
                and_(
                    Tenant.subscription_status == "active",
                    Tenant.subscription_starts_at >= start_date,
                    Tenant.subscription_starts_at <= end_date,
                )
            )
            upgrades_result = self.db.execute(upgrades_query)
            upgrades_count = upgrades_result.scalar() or 0

            # Downgrades (tier changes to lower tier)
            # Query subscription history for tier changes
            downgrades_query = select(func.count(distinct(SubscriptionHistory.tenant_id))).where(
                and_(
                    SubscriptionHistory.created_at >= start_date,
                    SubscriptionHistory.created_at <= end_date,
                    SubscriptionHistory.action.in_(["downgrade", "tier_change"]),
                )
            )
            downgrades_result = self.db.execute(downgrades_query)
            downgrades_count = downgrades_result.scalar() or 0

            # Daily signup trend (last 7 days for quick view)
            daily_trend_days = min(days, 7)
            daily_signups = []

            for i in range(daily_trend_days):
                day_start = end_date - timedelta(days=i)
                day_end = day_start + timedelta(days=1)

                day_query = select(func.count(Tenant.id)).where(
                    and_(
                        Tenant.created_at >= day_start,
                        Tenant.created_at < day_end,
                    )
                )
                day_result = self.db.execute(day_query)
                day_count = day_result.scalar() or 0

                daily_signups.append(
                    {
                        "date": day_start.date().isoformat(),
                        "count": day_count,
                    }
                )

            daily_signups.reverse()  # Oldest to newest

            # Growth rate calculation
            previous_period_start = start_date - timedelta(days=days)
            previous_signups_query = select(func.count(Tenant.id)).where(
                and_(
                    Tenant.created_at >= previous_period_start,
                    Tenant.created_at < start_date,
                )
            )
            previous_result = self.db.execute(previous_signups_query)
            previous_signups = previous_result.scalar() or 0

            signup_growth_rate = (
                ((new_signups - previous_signups) / previous_signups * 100)
                if previous_signups > 0
                else 100 if new_signups > 0 else 0
            )

            return {
                "new_signups": new_signups,
                "upgrades": upgrades_count,
                "downgrades": downgrades_count,
                "signup_growth_rate": float(signup_growth_rate),
                "daily_trend": daily_signups,
                "period_days": days,
            }

        except Exception as e:
            logger.error(f"Error calculating growth metrics: {e}")
            raise

    def get_usage_metrics(self) -> dict[str, Any]:
        """
        Get platform-wide usage metrics.

        Returns:
            Dict with conversation, ticket, and knowledge source counts
        """
        try:
            # Total conversations
            conversations_query = select(func.count(ConversationSession.id))
            conv_result = self.db.execute(conversations_query)
            total_conversations = conv_result.scalar() or 0

            # Active conversations (last 30 days)
            thirty_days_ago = datetime.utcnow() - timedelta(days=30)
            active_conv_query = select(func.count(ConversationSession.id)).where(
                ConversationSession.created_at >= thirty_days_ago
            )
            active_conv_result = self.db.execute(active_conv_query)
            active_conversations = active_conv_result.scalar() or 0

            # Total tickets
            tickets_query = select(func.count(Ticket.id))
            tickets_result = self.db.execute(tickets_query)
            total_tickets = tickets_result.scalar() or 0

            # Active tickets (unresolved)
            active_tickets_query = select(func.count(Ticket.id)).where(Ticket.status != "resolved")
            active_tickets_result = self.db.execute(active_tickets_query)
            active_tickets = active_tickets_result.scalar() or 0

            # Total knowledge sources
            knowledge_query = select(func.count(KnowledgeSource.id))
            knowledge_result = self.db.execute(knowledge_query)
            total_knowledge = knowledge_result.scalar() or 0

            # Average conversations per tenant
            active_tenants_query = select(func.count(Tenant.id)).where(
                Tenant.subscription_status.in_(["active", "trialing"])
            )
            active_tenants_result = self.db.execute(active_tenants_query)
            active_tenants = active_tenants_result.scalar() or 1  # Avoid division by zero

            avg_conversations_per_tenant = active_conversations / active_tenants
            visit_summary = VisitAnalyticsService(self.db).get_platform_daily_summary(days=7)

            return {
                "total_conversations": total_conversations,
                "active_conversations_30d": active_conversations,
                "total_tickets": total_tickets,
                "active_tickets": active_tickets,
                "total_knowledge_sources": total_knowledge,
                "avg_conversations_per_tenant": float(avg_conversations_per_tenant),
                "active_tenants": active_tenants,
                "daily_unique_visitors_today": visit_summary["today_unique_visitors"],
                "daily_total_visits_today": visit_summary["today_total_visits"],
                "daily_unique_visitors_last_7d": visit_summary["last_7d_unique_visitors"],
                "daily_visitors_trend": visit_summary["daily_trend"],
            }

        except Exception as e:
            logger.error(f"Error calculating usage metrics: {e}")
            raise

    def get_cohort_analysis(
        self,
        cohort_size: str = "month",
        months_back: int = 6,
    ) -> dict[str, Any]:
        """
        Get cohort retention analysis.

        Args:
            cohort_size: Size of cohort ('week' or 'month')
            months_back: How many months to analyze

        Returns:
            Dict with cohort retention data
        """
        try:
            end_date = datetime.utcnow()
            start_date = end_date - timedelta(days=months_back * 30)

            # Get all tenants grouped by signup month
            cohorts_query = (
                select(
                    func.date_trunc(cohort_size, Tenant.created_at).label("cohort"),
                    Tenant.id,
                    Tenant.subscription_status,
                    Tenant.created_at,
                )
                .where(Tenant.created_at >= start_date)
                .order_by("cohort")
            )

            result = self.db.execute(cohorts_query)
            tenant_data = result.all()

            # Group by cohort
            cohorts = {}
            for cohort_date, _tenant_id, status, _created_at in tenant_data:
                cohort_key = cohort_date.strftime("%Y-%m" if cohort_size == "month" else "%Y-W%U")
                if cohort_key not in cohorts:
                    cohorts[cohort_key] = {
                        "total": 0,
                        "active": 0,
                        "churned": 0,
                    }

                cohorts[cohort_key]["total"] += 1
                if status == "active":
                    cohorts[cohort_key]["active"] += 1
                elif status in ["canceled", "trial_expired"]:
                    cohorts[cohort_key]["churned"] += 1

            # Calculate retention rates
            cohort_analysis = []
            for cohort_key, data in cohorts.items():
                retention_rate = (data["active"] / data["total"] * 100) if data["total"] > 0 else 0
                churn_rate = (data["churned"] / data["total"] * 100) if data["total"] > 0 else 0

                cohort_analysis.append(
                    {
                        "cohort": cohort_key,
                        "total_signups": data["total"],
                        "active_count": data["active"],
                        "churned_count": data["churned"],
                        "retention_rate": float(retention_rate),
                        "churn_rate": float(churn_rate),
                    }
                )

            return {
                "cohorts": cohort_analysis,
                "cohort_size": cohort_size,
                "months_analyzed": months_back,
                "period": {
                    "start": start_date.isoformat(),
                    "end": end_date.isoformat(),
                },
            }

        except Exception as e:
            logger.error(f"Error calculating cohort analysis: {e}")
            raise

    def get_dashboard_summary(self) -> dict[str, Any]:
        """
        Get a comprehensive dashboard summary with all key metrics.

        Returns:
            Dict with all platform metrics
        """
        try:
            revenue_metrics = self.get_revenue_metrics()
            subscription_metrics = self.get_subscription_metrics()
            growth_metrics = self.get_growth_metrics()
            usage_metrics = self.get_usage_metrics()

            return {
                "revenue": revenue_metrics,
                "subscriptions": subscription_metrics,
                "growth": growth_metrics,
                "usage": usage_metrics,
                "generated_at": datetime.utcnow().isoformat(),
            }

        except Exception as e:
            logger.error(f"Error generating dashboard summary: {e}")
            raise
