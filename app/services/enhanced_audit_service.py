"""Enhanced audit service for platform-wide audit log management."""

import csv
import io
import json
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from loguru import logger
from sqlalchemy import and_, desc, func, select
from sqlalchemy.orm import Session

from app.db.models.audit import AuditLog
from app.db.models.tenant import Tenant


class EnhancedAuditService:
    """Service for platform-wide audit log querying and export."""

    def __init__(self, db: Session):
        """
        Initialize enhanced audit service.

        Args:
            db: Database session
        """
        self.db = db

    def log_action(
        self,
        action_type: str,
        performed_by_type: str,
        performed_by_id: UUID | None,
        performed_by_name: str | None,
        target_type: str,
        target_id: UUID | None,
        description: str | None = None,
        severity: str | None = None,
        metadata: dict[str, Any] | None = None,
        tenant_id: UUID | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> AuditLog:
        """
        Record a platform audit log entry.

        Args:
            action_type: Action identifier
            performed_by_type: Actor type (tenant_user, tenant_admin, platform_admin, system)
            performed_by_id: Actor ID (if applicable)
            performed_by_name: Actor display name/email
            target_type: Resource type
            target_id: Resource ID
            description: Human-readable description
            severity: Optional severity indicator
            metadata: Extra details/changes
            tenant_id: Tenant ID if applicable
            ip_address: IP address of actor
            user_agent: User agent string
        """
        actor_user_id = (
            performed_by_id
            if performed_by_type in {"user", "tenant_user", "tenant_admin"}
            else None
        )

        changes = {
            "description": description,
            "severity": severity,
            "performed_by_type": performed_by_type,
            "performed_by_id": str(performed_by_id) if performed_by_id else None,
            "performed_by_name": performed_by_name,
            "metadata": metadata or {},
        }

        audit_log = AuditLog(
            tenant_id=tenant_id,
            user_id=actor_user_id,
            action=action_type,
            resource_type=target_type,
            resource_id=target_id,
            changes=changes,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        self.db.add(audit_log)
        self.db.commit()
        self.db.refresh(audit_log)
        return audit_log

    def search_audit_logs(
        self,
        tenant_id: UUID | None = None,
        user_id: UUID | None = None,
        action: str | None = None,
        resource_type: str | None = None,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """
        Search audit logs with filters.

        Args:
            tenant_id: Filter by tenant (None for all tenants)
            user_id: Filter by user
            action: Filter by action type
            resource_type: Filter by resource type
            start_date: Filter by start date
            end_date: Filter by end date
            limit: Max results to return (default 100, max 1000)
            offset: Offset for pagination

        Returns:
            Dict with audit logs and pagination info
        """
        try:
            # Build query with filters
            query = select(AuditLog)

            # Apply filters
            filters = []

            if tenant_id:
                filters.append(AuditLog.tenant_id == tenant_id)

            if user_id:
                filters.append(AuditLog.user_id == user_id)

            if action:
                filters.append(AuditLog.action.ilike(f"%{action}%"))

            if resource_type:
                filters.append(AuditLog.resource_type.ilike(f"%{resource_type}%"))

            if start_date:
                filters.append(AuditLog.created_at >= start_date)

            if end_date:
                filters.append(AuditLog.created_at <= end_date)

            if filters:
                query = query.where(and_(*filters))

            # Get total count
            count_query = select(func.count()).select_from(AuditLog)
            if filters:
                count_query = count_query.where(and_(*filters))

            total_result = self.db.execute(count_query)
            total_count = total_result.scalar() or 0

            # Apply ordering, limit, and offset
            query = query.order_by(desc(AuditLog.created_at))
            query = query.limit(min(limit, 1000)).offset(offset)

            # Execute query
            result = self.db.execute(query)
            logs = result.scalars().all()

            # Format logs
            log_list = [
                {
                    "id": str(log.id),
                    "tenant_id": str(log.tenant_id) if log.tenant_id else None,
                    "user_id": str(log.user_id) if log.user_id else None,
                    "action": log.action,
                    "resource_type": log.resource_type,
                    "resource_id": str(log.resource_id) if log.resource_id else None,
                    "changes": log.changes,
                    "ip_address": log.ip_address,
                    "user_agent": log.user_agent,
                    "created_at": log.created_at.isoformat(),
                }
                for log in logs
            ]

            return {
                "logs": log_list,
                "pagination": {
                    "total": total_count,
                    "limit": limit,
                    "offset": offset,
                    "has_more": (offset + len(log_list)) < total_count,
                },
            }

        except Exception as e:
            logger.error(f"Error searching audit logs: {e}")
            raise

    def export_audit_logs(
        self,
        format: str = "csv",
        tenant_id: UUID | None = None,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
    ) -> str:
        """
        Export audit logs to CSV or JSON format.

        Args:
            format: Export format ('csv' or 'json')
            tenant_id: Filter by tenant (None for all)
            start_date: Filter by start date
            end_date: Filter by end date

        Returns:
            Exported data as string

        Raises:
            ValueError: If format is invalid
        """
        try:
            if format not in ["csv", "json"]:
                raise ValueError("Format must be 'csv' or 'json'")

            # Get all matching logs (with reasonable limit)
            search_result = self.search_audit_logs(
                tenant_id=tenant_id,
                start_date=start_date,
                end_date=end_date,
                limit=10000,  # Export limit
                offset=0,
            )

            logs = search_result["logs"]

            if format == "csv":
                return self._export_to_csv(logs)
            else:
                return self._export_to_json(logs)

        except Exception as e:
            logger.error(f"Error exporting audit logs: {e}")
            raise

    def _export_to_csv(self, logs: list[dict[str, Any]]) -> str:
        """Export logs to CSV format."""
        if not logs:
            return "No data to export"

        output = io.StringIO()
        fieldnames = [
            "id",
            "tenant_id",
            "user_id",
            "action",
            "resource_type",
            "resource_id",
            "changes",
            "ip_address",
            "user_agent",
            "created_at",
        ]

        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()

        for log in logs:
            # Convert changes dict to JSON string for CSV
            row = log.copy()
            if row.get("changes"):
                row["changes"] = json.dumps(row["changes"])
            writer.writerow(row)

        return output.getvalue()

    def _export_to_json(self, logs: list[dict[str, Any]]) -> str:
        """Export logs to JSON format."""
        return json.dumps(
            {
                "exported_at": datetime.utcnow().isoformat(),
                "total_records": len(logs),
                "logs": logs,
            },
            indent=2,
        )

    def get_tenant_activity_summary(
        self,
        tenant_id: UUID,
        days: int = 30,
    ) -> dict[str, Any]:
        """
        Get activity summary for a specific tenant.

        Args:
            tenant_id: Tenant ID
            days: Number of days to analyze

        Returns:
            Dict with activity summary
        """
        try:
            end_date = datetime.utcnow()
            start_date = end_date - timedelta(days=days)

            # Get tenant info
            tenant_query = select(Tenant).where(Tenant.id == tenant_id)
            tenant_result = self.db.execute(tenant_query)
            tenant = tenant_result.scalars().first()

            if not tenant:
                raise ValueError("Tenant not found")

            # Total actions
            total_query = select(func.count(AuditLog.id)).where(
                and_(
                    AuditLog.tenant_id == tenant_id,
                    AuditLog.created_at >= start_date,
                )
            )
            total_result = self.db.execute(total_query)
            total_actions = total_result.scalar() or 0

            # Actions by type
            actions_query = (
                select(AuditLog.action, func.count(AuditLog.id).label("count"))
                .where(
                    and_(
                        AuditLog.tenant_id == tenant_id,
                        AuditLog.created_at >= start_date,
                    )
                )
                .group_by(AuditLog.action)
            )

            actions_result = self.db.execute(actions_query)
            actions_breakdown: dict[str, int] = {
                str(action): int(count) for action, count in actions_result.all()
            }

            # Active users count
            users_query = select(func.count(func.distinct(AuditLog.user_id))).where(
                and_(
                    AuditLog.tenant_id == tenant_id,
                    AuditLog.created_at >= start_date,
                    AuditLog.user_id.isnot(None),
                )
            )
            users_result = self.db.execute(users_query)
            active_users = users_result.scalar() or 0

            # Daily activity trend
            daily_activity = {}
            current_date = start_date.date()
            end = end_date.date()

            while current_date <= end:
                daily_activity[current_date.isoformat()] = 0
                current_date += timedelta(days=1)

            # Get daily counts
            for day_key in daily_activity.keys():
                day_start = datetime.fromisoformat(day_key)
                day_end = day_start + timedelta(days=1)

                day_query = select(func.count(AuditLog.id)).where(
                    and_(
                        AuditLog.tenant_id == tenant_id,
                        AuditLog.created_at >= day_start,
                        AuditLog.created_at < day_end,
                    )
                )
                day_result = self.db.execute(day_query)
                daily_activity[day_key] = day_result.scalar() or 0

            return {
                "tenant_id": str(tenant_id),
                "tenant_name": tenant.name,
                "period_days": days,
                "total_actions": total_actions,
                "active_users": active_users,
                "actions_breakdown": actions_breakdown,
                "daily_activity": daily_activity,
            }

        except Exception as e:
            logger.error(f"Error getting tenant activity summary: {e}")
            raise

    def get_security_events(
        self,
        severity: str | None = None,
        days: int = 7,
        limit: int = 100,
    ) -> dict[str, Any]:
        """
        Get security-related events from audit logs.

        Args:
            severity: Filter by severity (high, medium, low)
            days: Number of days to analyze
            limit: Max results to return

        Returns:
            Dict with security events
        """
        try:
            end_date = datetime.utcnow()
            start_date = end_date - timedelta(days=days)

            # Security-related actions
            security_actions = [
                "login_failed",
                "password_reset",
                "password_changed",
                "user_deactivated",
                "admin_created",
                "admin_deleted",
                "api_key_created",
                "api_key_revoked",
                "subscription_override",
            ]

            # Build query
            query = (
                select(AuditLog)
                .where(
                    and_(
                        AuditLog.created_at >= start_date,
                        AuditLog.action.in_(security_actions),
                    )
                )
                .order_by(desc(AuditLog.created_at))
                .limit(limit)
            )

            result = self.db.execute(query)
            events = result.scalars().all()

            # Classify severity
            high_severity_actions = [
                "admin_created",
                "admin_deleted",
                "user_deactivated",
                "subscription_override",
            ]
            medium_severity_actions = [
                "login_failed",
                "password_reset",
                "api_key_revoked",
            ]

            event_list = []
            for event in events:
                # Determine severity
                if event.action in high_severity_actions:
                    event_severity = "high"
                elif event.action in medium_severity_actions:
                    event_severity = "medium"
                else:
                    event_severity = "low"

                # Apply severity filter
                if severity and event_severity != severity:
                    continue

                event_list.append(
                    {
                        "id": str(event.id),
                        "severity": event_severity,
                        "action": event.action,
                        "tenant_id": str(event.tenant_id) if event.tenant_id else None,
                        "user_id": str(event.user_id) if event.user_id else None,
                        "resource_type": event.resource_type,
                        "changes": event.changes,
                        "ip_address": event.ip_address,
                        "created_at": event.created_at.isoformat(),
                    }
                )

            # Count by severity
            severity_counts: dict[str, int] = {"high": 0, "medium": 0, "low": 0}
            for evt in event_list:
                severity = str(evt.get("severity", "low"))
                if severity in severity_counts:
                    severity_counts[severity] += 1

            return {
                "period_days": days,
                "total_events": len(event_list),
                "severity_counts": severity_counts,
                "events": event_list[:limit],
            }

        except Exception as e:
            logger.error(f"Error getting security events: {e}")
            raise
