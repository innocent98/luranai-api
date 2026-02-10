"""Service for recording and aggregating visit analytics."""

from collections import defaultdict
from datetime import timedelta
from uuid import UUID

from sqlalchemy import and_, select
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import Session

from app.db.models.visit_analytics import DailyVisit
from app.core.logger import log
from app.utils.datetime import utcnow_naive


class VisitAnalyticsService:
    """Track dashboard/chat visits and expose daily aggregates."""

    def __init__(self, db: Session):
        self.db = db

    def record_visit(
        self,
        *,
        source: str,
        visitor_key: str,
        tenant_id: UUID | None = None,
        meta: dict | None = None,
    ) -> None:
        """Record one visit event into a daily per-visitor bucket."""
        now = utcnow_naive()
        visit_date = now.date()
        normalized_source = source.strip().lower()
        normalized_key = visitor_key.strip()[:128]

        if not normalized_source or not normalized_key:
            return

        try:
            stmt = select(DailyVisit).where(
                and_(
                    DailyVisit.visit_date == visit_date,
                    DailyVisit.tenant_id == tenant_id,
                    DailyVisit.source == normalized_source,
                    DailyVisit.visitor_key == normalized_key,
                )
            )
            visit = self.db.execute(stmt).scalar_one_or_none()

            if visit:
                visit.visit_count += 1
                visit.last_seen_at = now
                if meta:
                    visit.meta = {**(visit.meta or {}), **meta}
            else:
                self.db.add(
                    DailyVisit(
                        visit_date=visit_date,
                        tenant_id=tenant_id,
                        source=normalized_source,
                        visitor_key=normalized_key,
                        visit_count=1,
                        first_seen_at=now,
                        last_seen_at=now,
                        meta=meta or {},
                    )
                )

            self.db.commit()
        except (ProgrammingError, OperationalError) as e:
            self.db.rollback()
            if "daily_visits" in str(e):
                log.warning("Visit tracking skipped because daily_visits table is not available yet")
                return
            raise

    def get_platform_daily_summary(self, days: int = 7) -> dict:
        """Get platform daily visitors/visits summary for admin dashboards."""
        now = utcnow_naive().date()
        start_date = now - timedelta(days=max(days - 1, 0))

        # Row-level visitor buckets (already unique per day/source/tenant/visitor).
        try:
            stmt = select(DailyVisit).where(
                and_(
                    DailyVisit.visit_date >= start_date,
                    DailyVisit.visit_date <= now,
                )
            )
            rows = self.db.execute(stmt).scalars().all()
        except (ProgrammingError, OperationalError) as e:
            self.db.rollback()
            if "daily_visits" in str(e):
                log.warning(
                    "Daily visit summary requested before migration; returning empty summary"
                )
                return {
                    "today_unique_visitors": 0,
                    "yesterday_unique_visitors": 0,
                    "today_total_visits": 0,
                    "last_7d_unique_visitors": 0,
                    "daily_trend": [
                        {
                            "date": (start_date + timedelta(days=offset)).isoformat(),
                            "unique_visitors": 0,
                            "dashboard_unique_visitors": 0,
                            "chat_widget_unique_visitors": 0,
                            "total_visits": 0,
                        }
                        for offset in range(days)
                    ],
                }
            raise

        by_date: dict = defaultdict(
            lambda: {
                "unique_visitors": set(),
                "dashboard_visitors": set(),
                "chat_widget_visitors": set(),
                "total_visits": 0,
            }
        )

        for row in rows:
            key = row.visit_date.isoformat()
            visitor_fingerprint = f"{row.source}:{row.tenant_id}:{row.visitor_key}"
            by_date[key]["unique_visitors"].add(visitor_fingerprint)
            by_date[key]["total_visits"] += row.visit_count
            if row.source == "dashboard":
                by_date[key]["dashboard_visitors"].add(visitor_fingerprint)
            elif row.source == "chat_widget":
                by_date[key]["chat_widget_visitors"].add(visitor_fingerprint)

        trend = []
        for offset in range(days):
            day = start_date + timedelta(days=offset)
            key = day.isoformat()
            day_stats = by_date[key]
            trend.append(
                {
                    "date": key,
                    "unique_visitors": len(day_stats["unique_visitors"]),
                    "dashboard_unique_visitors": len(day_stats["dashboard_visitors"]),
                    "chat_widget_unique_visitors": len(day_stats["chat_widget_visitors"]),
                    "total_visits": day_stats["total_visits"],
                }
            )

        today_key = now.isoformat()
        yesterday_key = (now - timedelta(days=1)).isoformat()
        today_unique = len(by_date[today_key]["unique_visitors"])
        yesterday_unique = len(by_date[yesterday_key]["unique_visitors"])

        return {
            "today_unique_visitors": today_unique,
            "yesterday_unique_visitors": yesterday_unique,
            "today_total_visits": by_date[today_key]["total_visits"],
            "last_7d_unique_visitors": sum(item["unique_visitors"] for item in trend),
            "daily_trend": trend,
        }
