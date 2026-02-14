"""Platform-wide analytics service for admin dashboards."""

from datetime import datetime, timedelta
from typing import Any

from loguru import logger
from sqlalchemy import String, and_, cast, distinct, func, or_, select
from sqlalchemy.orm import Session

from app.db.models.conversation import ConversationSession, Message, MessageRole, SessionStatus, Ticket
from app.db.models.flow import FlowExecution, FlowExecutionStatus
from app.db.models.knowledge import KnowledgeSource
from app.db.models.subscription_plan import SubscriptionPlan
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

            # Pricing from DB plans (fallback to 0 when unavailable).
            plans = self.db.execute(
                select(
                    SubscriptionPlan.tier,
                    SubscriptionPlan.monthly_price_kobo,
                    SubscriptionPlan.annual_price_kobo,
                ).where(SubscriptionPlan.is_active.is_(True))
            ).all()
            pricing = {
                str(tier).lower(): {
                    "monthly": int(monthly_price_kobo or 0) / 100,
                    "annual": int(annual_price_kobo or 0) / 100,
                }
                for tier, monthly_price_kobo, annual_price_kobo in plans
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

    def get_ai_flow_comparison_metrics(self, days: int = 30) -> dict[str, Any]:
        """
        Compare AI/RAG and Flow performance across the platform.

        Args:
            days: Lookback period in days
        """
        try:
            def _percentile(values: list[float], p: float) -> float:
                if not values:
                    return 0.0
                ordered = sorted(values)
                if len(ordered) == 1:
                    return float(ordered[0])
                rank = (len(ordered) - 1) * p
                low = int(rank)
                high = min(low + 1, len(ordered) - 1)
                frac = rank - low
                return float(ordered[low] + (ordered[high] - ordered[low]) * frac)

            end_date = datetime.utcnow()
            start_date = end_date - timedelta(days=days)
            flow_flag_expr = func.lower(
                func.replace(
                    func.coalesce(cast(Message.meta["flow_triggered"], String), ""),
                    '"',
                    "",
                )
            )

            # Interaction counts from assistant messages.
            ai_interactions_query = select(func.count(Message.id)).where(
                and_(
                    Message.role == MessageRole.ASSISTANT,
                    Message.created_at >= start_date,
                    or_(
                        Message.meta.is_(None),
                        flow_flag_expr != "true",
                    ),
                )
            )
            ai_interactions = int(self.db.execute(ai_interactions_query).scalar() or 0)

            flow_interactions_query = select(func.count(Message.id)).where(
                and_(
                    Message.role == MessageRole.ASSISTANT,
                    Message.created_at >= start_date,
                    flow_flag_expr == "true",
                )
            )
            flow_interactions = int(self.db.execute(flow_interactions_query).scalar() or 0)

            # Flow execution performance.
            flow_exec_total = int(
                self.db.execute(
                    select(func.count(FlowExecution.id)).where(FlowExecution.created_at >= start_date)
                ).scalar()
                or 0
            )
            flow_exec_completed = int(
                self.db.execute(
                    select(func.count(FlowExecution.id)).where(
                        and_(
                            FlowExecution.created_at >= start_date,
                            FlowExecution.status == FlowExecutionStatus.COMPLETED,
                        )
                    )
                ).scalar()
                or 0
            )
            flow_exec_failed = int(
                self.db.execute(
                    select(func.count(FlowExecution.id)).where(
                        and_(
                            FlowExecution.created_at >= start_date,
                            FlowExecution.status == FlowExecutionStatus.FAILED,
                        )
                    )
                ).scalar()
                or 0
            )

            # Session classification for escalation comparison.
            flow_session_subquery = (
                select(FlowExecution.session_id.label("session_id"))
                .where(FlowExecution.created_at >= start_date)
                .distinct()
                .subquery()
            )
            ai_session_subquery = (
                select(Message.session_id.label("session_id"))
                .where(
                    and_(
                        Message.role == MessageRole.ASSISTANT,
                        Message.created_at >= start_date,
                        or_(
                            Message.meta.is_(None),
                            flow_flag_expr != "true",
                        ),
                        ~Message.session_id.in_(select(flow_session_subquery.c.session_id)),
                    )
                )
                .distinct()
                .subquery()
            )

            flow_escalations = int(
                self.db.execute(
                    select(func.count(Ticket.id)).where(
                        and_(
                            Ticket.created_at >= start_date,
                            Ticket.session_id.in_(select(flow_session_subquery.c.session_id)),
                        )
                    )
                ).scalar()
                or 0
            )
            ai_escalations = int(
                self.db.execute(
                    select(func.count(Ticket.id)).where(
                        and_(
                            Ticket.created_at >= start_date,
                            Ticket.session_id.in_(select(ai_session_subquery.c.session_id)),
                        )
                    )
                ).scalar()
                or 0
            )

            ai_sessions_total = int(
                self.db.execute(select(func.count()).select_from(ai_session_subquery)).scalar() or 0
            )
            ai_sessions_resolved = int(
                self.db.execute(
                    select(func.count(ConversationSession.id)).where(
                        and_(
                            ConversationSession.id.in_(select(ai_session_subquery.c.session_id)),
                            ConversationSession.status == SessionStatus.RESOLVED,
                        )
                    )
                ).scalar()
                or 0
            )

            ai_resolution_rate = (
                (ai_sessions_resolved / ai_sessions_total * 100) if ai_sessions_total > 0 else 0
            )
            flow_completion_rate = (
                (flow_exec_completed / flow_exec_total * 100) if flow_exec_total > 0 else 0
            )
            flow_failure_rate = (flow_exec_failed / flow_exec_total * 100) if flow_exec_total > 0 else 0

            # Daily AI vs Flow interaction trend.
            daily_rows = self.db.execute(
                select(
                    func.date(Message.created_at).label("day"),
                    flow_flag_expr.label("flow_flag"),
                    func.count(Message.id).label("count"),
                ).where(
                    and_(
                        Message.role == MessageRole.ASSISTANT,
                        Message.created_at >= start_date,
                    )
                )
                .group_by(func.date(Message.created_at), flow_flag_expr)
                .order_by(func.date(Message.created_at).asc())
            ).all()

            trend_map: dict[str, dict[str, int]] = {}
            for day, flow_flag, count in daily_rows:
                day_key = day.isoformat() if hasattr(day, "isoformat") else str(day)
                if day_key not in trend_map:
                    trend_map[day_key] = {"ai_interactions": 0, "flow_interactions": 0}
                if flow_flag == "true":
                    trend_map[day_key]["flow_interactions"] += int(count or 0)
                else:
                    trend_map[day_key]["ai_interactions"] += int(count or 0)

            daily_trend = [
                {
                    "date": day_key,
                    "ai_interactions": values["ai_interactions"],
                    "flow_interactions": values["flow_interactions"],
                }
                for day_key, values in sorted(trend_map.items(), key=lambda x: x[0])
            ]

            # Top tenants by interaction volume (AI + Flow).
            tenant_rows = self.db.execute(
                select(
                    Tenant.id,
                    Tenant.name,
                    flow_flag_expr.label("flow_flag"),
                    func.count(Message.id).label("count"),
                )
                .select_from(Message)
                .join(ConversationSession, Message.session_id == ConversationSession.id)
                .join(Tenant, ConversationSession.tenant_id == Tenant.id)
                .where(
                    and_(
                        Message.role == MessageRole.ASSISTANT,
                        Message.created_at >= start_date,
                    )
                )
                .group_by(Tenant.id, Tenant.name, flow_flag_expr)
            ).all()

            tenant_map: dict[str, dict[str, Any]] = {}
            for tenant_id, tenant_name, flow_flag, count in tenant_rows:
                tkey = str(tenant_id)
                if tkey not in tenant_map:
                    tenant_map[tkey] = {
                        "tenant_id": tkey,
                        "tenant_name": tenant_name,
                        "ai_interactions": 0,
                        "flow_interactions": 0,
                        "total_interactions": 0,
                    }
                if flow_flag == "true":
                    tenant_map[tkey]["flow_interactions"] += int(count or 0)
                else:
                    tenant_map[tkey]["ai_interactions"] += int(count or 0)
                tenant_map[tkey]["total_interactions"] = (
                    tenant_map[tkey]["ai_interactions"] + tenant_map[tkey]["flow_interactions"]
                )

            top_tenants = sorted(
                tenant_map.values(), key=lambda row: row["total_interactions"], reverse=True
            )[:10]
            for tenant in top_tenants:
                total = tenant["total_interactions"] or 1
                tenant["flow_share_percentage"] = round(tenant["flow_interactions"] / total * 100, 1)

            # AI/RAG quality counters from assistant message metadata.
            ai_meta_rows = self.db.execute(
                select(Message.session_id, Message.created_at, Message.meta).where(
                    and_(
                        Message.role == MessageRole.ASSISTANT,
                        Message.created_at >= start_date,
                        or_(
                            Message.meta.is_(None),
                            flow_flag_expr != "true",
                        ),
                    )
                )
            ).all()
            ai_meta = [row[2] for row in ai_meta_rows if isinstance(row[2], dict)]
            hit_count = sum(1 for meta in ai_meta if meta.get("retrieval_hit") is True)
            hit_rate = (hit_count / len(ai_meta) * 100) if ai_meta else 0.0
            miss_count = max(len(ai_meta) - hit_count, 0)
            miss_rate = (miss_count / len(ai_meta) * 100) if ai_meta else 0.0

            similarity_values = [
                float(meta.get("avg_similarity", 0.0))
                for meta in ai_meta
                if meta.get("avg_similarity") is not None
            ]
            avg_similarity = (
                sum(similarity_values) / len(similarity_values) if similarity_values else 0.0
            )
            similarity_hit_values = [
                float(meta.get("avg_similarity", 0.0))
                for meta in ai_meta
                if meta.get("retrieval_hit") is True and meta.get("avg_similarity") is not None
            ]
            similarity_miss_values = [
                float(meta.get("avg_similarity", 0.0))
                for meta in ai_meta
                if meta.get("retrieval_hit") is False and meta.get("avg_similarity") is not None
            ]

            prompt_tokens = [
                float((meta.get("token_breakdown") or {}).get("prompt_tokens", 0))
                for meta in ai_meta
                if isinstance(meta.get("token_breakdown"), dict)
                and (meta.get("token_breakdown") or {}).get("prompt_tokens") is not None
            ]
            completion_tokens = [
                float((meta.get("token_breakdown") or {}).get("completion_tokens", 0))
                for meta in ai_meta
                if isinstance(meta.get("token_breakdown"), dict)
                and (meta.get("token_breakdown") or {}).get("completion_tokens") is not None
            ]
            total_tokens = [
                float((meta.get("token_breakdown") or {}).get("total_tokens", 0))
                for meta in ai_meta
                if isinstance(meta.get("token_breakdown"), dict)
                and (meta.get("token_breakdown") or {}).get("total_tokens") is not None
            ]
            uncertainty_count = sum(
                1
                for meta in ai_meta
                if (meta.get("response_quality") or {}).get("has_uncertainty") is True
            )
            uncertainty_rate = (uncertainty_count / len(ai_meta) * 100) if ai_meta else 0.0

            # "Isolated sample" proxy: first assistant response per session in period.
            first_meta_by_session: dict[str, tuple[datetime, dict[str, Any]]] = {}
            for session_id, created_at, meta in ai_meta_rows:
                if not isinstance(meta, dict):
                    continue
                key = str(session_id)
                existing = first_meta_by_session.get(key)
                if existing is None or created_at < existing[0]:
                    first_meta_by_session[key] = (created_at, meta)

            first_turn_prompt_tokens = [
                float((meta.get("token_breakdown") or {}).get("prompt_tokens", 0))
                for _, meta in first_meta_by_session.values()
                if isinstance(meta.get("token_breakdown"), dict)
                and (meta.get("token_breakdown") or {}).get("prompt_tokens") is not None
            ]
            first_turn_completion_tokens = [
                float((meta.get("token_breakdown") or {}).get("completion_tokens", 0))
                for _, meta in first_meta_by_session.values()
                if isinstance(meta.get("token_breakdown"), dict)
                and (meta.get("token_breakdown") or {}).get("completion_tokens") is not None
            ]
            first_turn_total_tokens = [
                float((meta.get("token_breakdown") or {}).get("total_tokens", 0))
                for _, meta in first_meta_by_session.values()
                if isinstance(meta.get("token_breakdown"), dict)
                and (meta.get("token_breakdown") or {}).get("total_tokens") is not None
            ]
            escalated_after_answer = sum(
                1
                for meta in ai_meta
                if meta.get("escalation_action") in {"offer_escalation", "auto_escalate"}
                and (meta.get("response_quality") or {}).get("is_substantial") is True
            )
            escalated_after_answer_rate = (
                (escalated_after_answer / len(ai_meta) * 100) if ai_meta else 0.0
            )

            return {
                "period_days": days,
                "period": {
                    "start": start_date.isoformat(),
                    "end": end_date.isoformat(),
                },
                "kpis": {
                    "ai_interactions": ai_interactions,
                    "flow_interactions": flow_interactions,
                    "ai_escalations": ai_escalations,
                    "flow_escalations": flow_escalations,
                    "ai_resolution_rate": round(ai_resolution_rate, 2),
                    "flow_completion_rate": round(flow_completion_rate, 2),
                    "flow_failure_rate": round(flow_failure_rate, 2),
                },
                "outcomes": {
                    "ai": {
                        "resolved": ai_sessions_resolved,
                        "escalated": ai_escalations,
                    },
                    "flow": {
                        "completed": flow_exec_completed,
                        "failed": flow_exec_failed,
                        "escalated": flow_escalations,
                    },
                },
                "rag_quality": {
                    "retrieval_hit_rate": round(hit_rate, 2),
                    "retrieval_miss_rate": round(miss_rate, 2),
                    "avg_similarity": round(avg_similarity, 3),
                    "avg_similarity_hit": round(
                        (sum(similarity_hit_values) / len(similarity_hit_values))
                        if similarity_hit_values
                        else 0.0,
                        3,
                    ),
                    "avg_similarity_miss": round(
                        (sum(similarity_miss_values) / len(similarity_miss_values))
                        if similarity_miss_values
                        else 0.0,
                        3,
                    ),
                    "uncertainty_rate": round(uncertainty_rate, 2),
                    "escalation_after_answer_rate": round(escalated_after_answer_rate, 2),
                    "prompt_tokens_total": int(round(sum(prompt_tokens))) if prompt_tokens else 0,
                    "completion_tokens_total": (
                        int(round(sum(completion_tokens))) if completion_tokens else 0
                    ),
                    "total_tokens_total": int(round(sum(total_tokens))) if total_tokens else 0,
                    "prompt_tokens_avg": (
                        round(sum(prompt_tokens) / len(prompt_tokens), 1) if prompt_tokens else 0.0
                    ),
                    "completion_tokens_avg": (
                        round(sum(completion_tokens) / len(completion_tokens), 1)
                        if completion_tokens
                        else 0.0
                    ),
                    "total_tokens_avg": (
                        round(sum(total_tokens) / len(total_tokens), 1) if total_tokens else 0.0
                    ),
                    "first_turn_prompt_tokens_avg": (
                        round(sum(first_turn_prompt_tokens) / len(first_turn_prompt_tokens), 1)
                        if first_turn_prompt_tokens
                        else 0.0
                    ),
                    "first_turn_completion_tokens_avg": (
                        round(
                            sum(first_turn_completion_tokens) / len(first_turn_completion_tokens), 1
                        )
                        if first_turn_completion_tokens
                        else 0.0
                    ),
                    "first_turn_total_tokens_avg": (
                        round(sum(first_turn_total_tokens) / len(first_turn_total_tokens), 1)
                        if first_turn_total_tokens
                        else 0.0
                    ),
                    "prompt_tokens_p50": int(round(_percentile(prompt_tokens, 0.50))),
                    "prompt_tokens_p95": int(round(_percentile(prompt_tokens, 0.95))),
                    "samples": len(ai_meta),
                },
                "daily_trend": daily_trend,
                "top_tenants": top_tenants,
            }

        except Exception as e:
            logger.error(f"Error calculating AI/Flow comparison metrics: {e}")
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
