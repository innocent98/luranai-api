"""System health monitoring service for platform admins."""

import os
from datetime import datetime, timedelta
from typing import Any

import redis
from loguru import logger
from sqlalchemy import and_, func, select, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models.audit import AuditLog
from app.db.models.job import JobStatus, ProcessingJob


class SystemHealthService:
    """Service for monitoring system health and performance."""

    def __init__(self, db: Session):
        """
        Initialize system health service.

        Args:
            db: Database session
        """
        self.db = db
        self._redis_client: redis.Redis | None = None

    @property
    def redis_client(self) -> redis.Redis:
        """Get Redis client (lazy initialization)."""
        if self._redis_client is None:
            redis_url = os.getenv("REDIS_URL", settings.REDIS_URL)
            self._redis_client = redis.from_url(redis_url, decode_responses=True)
        return self._redis_client

    def get_job_status(self, hours: int = 24) -> dict[str, Any]:
        """
        Get background job statistics.

        Args:
            hours: Number of hours to analyze (default 24)

        Returns:
            Dict with job statistics and health status
        """
        try:
            cutoff_time = datetime.utcnow() - timedelta(hours=hours)

            # Total jobs by status
            status_query = (
                select(ProcessingJob.status, func.count(ProcessingJob.id).label("count"))
                .where(ProcessingJob.created_at >= cutoff_time)
                .group_by(ProcessingJob.status)
            )

            result = self.db.execute(status_query)
            status_counts: dict[JobStatus, int] = {
                status: int(count) for status, count in result.all()
            }

            # Recent failures
            failures_query = (
                select(ProcessingJob)
                .where(
                    and_(
                        ProcessingJob.status == JobStatus.FAILED,
                        ProcessingJob.created_at >= cutoff_time,
                    )
                )
                .order_by(
                    ProcessingJob.completed_at.desc().nullslast(), ProcessingJob.created_at.desc()
                )
                .limit(10)
            )

            failures_result = self.db.execute(failures_query)
            recent_failures = failures_result.scalars().all()

            failure_details = [
                {
                    "id": str(job.id),
                    "job_type": job.job_type.value,
                    "tenant_id": str(job.tenant_id),
                    "error_message": job.error_message[:200] if job.error_message else None,
                    "created_at": job.created_at.isoformat(),
                    "completed_at": job.completed_at.isoformat() if job.completed_at else None,
                }
                for job in recent_failures
            ]

            # Calculate success rate
            total_completed = status_counts.get(JobStatus.COMPLETED, 0) + status_counts.get(
                JobStatus.FAILED, 0
            )
            success_rate = (
                (status_counts.get(JobStatus.COMPLETED, 0) / total_completed * 100)
                if total_completed > 0
                else 100
            )

            # Jobs by type
            type_query = (
                select(ProcessingJob.job_type, func.count(ProcessingJob.id).label("count"))
                .where(ProcessingJob.created_at >= cutoff_time)
                .group_by(ProcessingJob.job_type)
            )

            type_result = self.db.execute(type_query)
            jobs_by_type = {job_type.value: int(count) for job_type, count in type_result.all()}

            return {
                "period_hours": hours,
                "status_counts": {
                    "queued": status_counts.get(JobStatus.QUEUED, 0),
                    "processing": status_counts.get(JobStatus.PROCESSING, 0),
                    "completed": status_counts.get(JobStatus.COMPLETED, 0),
                    "failed": status_counts.get(JobStatus.FAILED, 0),
                    "cancelled": status_counts.get(JobStatus.CANCELLED, 0),
                },
                "success_rate": float(success_rate),
                "jobs_by_type": jobs_by_type,
                "recent_failures": failure_details,
                "health_status": (
                    "healthy"
                    if success_rate >= 95
                    else "degraded" if success_rate >= 85 else "unhealthy"
                ),
            }

        except Exception as e:
            logger.error(f"Error getting job status: {e}")
            raise

    def get_api_health(self, hours: int = 1) -> dict[str, Any]:
        """
        Get API health metrics from audit logs.

        Args:
            hours: Number of hours to analyze (default 1)

        Returns:
            Dict with API performance metrics
        """
        try:
            cutoff_time = datetime.utcnow() - timedelta(hours=hours)

            # Count API actions by type
            actions_query = (
                select(AuditLog.action, func.count(AuditLog.id).label("count"))
                .where(AuditLog.created_at >= cutoff_time)
                .group_by(AuditLog.action)
            )

            result = self.db.execute(actions_query)
            actions = result.all()

            # Total requests
            total_requests_query = select(func.count(AuditLog.id)).where(
                AuditLog.created_at >= cutoff_time
            )
            total_result = self.db.execute(total_requests_query)
            total_requests = total_result.scalar() or 0

            # Calculate requests per minute
            requests_per_minute = total_requests / (hours * 60) if hours > 0 else 0

            # Top actions
            action_rows = [(str(action), int(count)) for action, count in actions]
            top_actions = sorted(action_rows, key=lambda x: x[1], reverse=True)[:10]
            top_actions_list = [{"action": action, "count": count} for action, count in top_actions]

            # Estimate error rate (actions with "error" or "failed" in name)
            error_actions = [
                (action, count)
                for action, count in action_rows
                if "error" in action.lower() or "failed" in action.lower()
            ]
            error_count = sum(count for _, count in error_actions)
            error_rate = (error_count / total_requests * 100) if total_requests > 0 else 0

            return {
                "period_hours": hours,
                "total_requests": total_requests,
                "requests_per_minute": float(requests_per_minute),
                "error_rate": float(error_rate),
                "top_actions": top_actions_list,
                "health_status": (
                    "healthy" if error_rate < 1 else "degraded" if error_rate < 5 else "unhealthy"
                ),
            }

        except Exception as e:
            logger.error(f"Error getting API health: {e}")
            raise

    def get_database_health(self) -> dict[str, Any]:
        """
        Get database performance metrics.

        Returns:
            Dict with database health information
        """
        try:
            # Database connection pool stats (from SQLAlchemy)
            bind = self.db.get_bind()
            pool = getattr(bind, "pool", None)
            pool_stats = (
                {
                    "size": pool.size(),
                    "checked_in": pool.checkedin(),
                    "checked_out": pool.checkedout(),
                    "overflow": pool.overflow(),
                    "max_overflow": pool._max_overflow,
                }
                if pool
                else {}
            )

            # Calculate pool utilization against configured max capacity.
            # NOTE: pool.overflow() can be negative when below pool_size, so it should
            # not be used as the denominator for utilization.
            max_capacity = (pool.size() + pool._max_overflow) if pool else 0
            in_use = pool.checkedout() if pool else 0
            utilization = (in_use / max_capacity * 100) if max_capacity > 0 else 0

            # Test database response time
            start_time = datetime.utcnow()
            self.db.execute(text("SELECT 1"))
            response_time_ms = (datetime.utcnow() - start_time).total_seconds() * 1000

            # Get database size (PostgreSQL specific)
            try:
                size_query = text("SELECT pg_database_size(current_database()) as size")
                size_result = self.db.execute(size_query)
                db_size_bytes = size_result.scalar()
                db_size_mb = db_size_bytes / (1024 * 1024) if db_size_bytes else 0
            except Exception as e:
                logger.warning(f"Could not get database size: {e}")
                db_size_mb = 0

            # Count total records across main tables
            try:
                tenant_count_query = select(func.count()).select_from(text("tenants"))
                tenant_count = self.db.execute(tenant_count_query).scalar() or 0

                user_count_query = select(func.count()).select_from(text("users"))
                user_count = self.db.execute(user_count_query).scalar() or 0

                conversation_count_query = select(func.count()).select_from(
                    text("conversation_sessions")
                )
                conversation_count = self.db.execute(conversation_count_query).scalar() or 0
            except Exception as e:
                logger.warning(f"Could not get record counts: {e}")
                tenant_count = user_count = conversation_count = 0

            return {
                "connection_pool": pool_stats,
                "pool_utilization_percent": float(utilization),
                "response_time_ms": float(response_time_ms),
                "database_size_mb": float(db_size_mb),
                "record_counts": {
                    "tenants": tenant_count,
                    "users": user_count,
                    "conversations": conversation_count,
                },
                "health_status": (
                    "healthy"
                    if utilization < 80 and response_time_ms < 100
                    else "degraded" if utilization < 95 and response_time_ms < 500 else "unhealthy"
                ),
            }

        except Exception as e:
            logger.error(f"Error getting database health: {e}")
            raise

    def get_queue_status(self) -> dict[str, Any]:
        """
        Get Redis/ARQ queue statistics.

        Returns:
            Dict with queue health information
        """
        try:
            redis_client: Any = self.redis_client

            # Test Redis connection
            start_time = datetime.utcnow()
            redis_client.ping()
            ping_time_ms = (datetime.utcnow() - start_time).total_seconds() * 1000

            # Get Redis info
            redis_info: dict[str, Any] = redis_client.info()

            # ARQ queue stats (approximate)
            # ARQ uses keys like "arq:queue" for the main queue
            try:
                queue_length = redis_client.llen("arq:queue") or 0
            except Exception:
                queue_length = 0

            # Get memory usage
            used_memory_mb = redis_info.get("used_memory", 0) / (1024 * 1024)
            max_memory = redis_info.get("maxmemory", 0)
            memory_utilization = (
                (redis_info.get("used_memory", 0) / max_memory * 100) if max_memory > 0 else 0
            )

            # Connected clients
            connected_clients = redis_info.get("connected_clients", 0)

            # Check for failed jobs in Redis (ARQ stores results)
            try:
                # Scan for ARQ result keys
                cursor = 0
                failed_jobs = 0
                scan_count = 0
                max_scans = 10  # Limit scanning to avoid performance issues

                while scan_count < max_scans:
                    cursor, keys = redis_client.scan(cursor, match="arq:result:*", count=100)
                    scan_count += 1

                    for key in keys:
                        try:
                            result = redis_client.get(key)
                            if result and "error" in result.lower():
                                failed_jobs += 1
                        except Exception:
                            pass

                    if cursor == 0:
                        break
            except Exception as e:
                logger.warning(f"Could not scan for failed jobs: {e}")
                failed_jobs = 0

            return {
                "redis_ping_ms": float(ping_time_ms),
                "queue_length": queue_length,
                "used_memory_mb": float(used_memory_mb),
                "memory_utilization_percent": float(memory_utilization),
                "connected_clients": connected_clients,
                "failed_jobs_cached": failed_jobs,
                "uptime_seconds": redis_info.get("uptime_in_seconds", 0),
                "health_status": (
                    "healthy"
                    if queue_length < 100 and ping_time_ms < 50
                    else "degraded" if queue_length < 500 and ping_time_ms < 200 else "unhealthy"
                ),
            }

        except Exception as e:
            logger.error(f"Error getting queue status: {e}")
            return {
                "redis_ping_ms": 0,
                "queue_length": 0,
                "health_status": "unavailable",
                "error": str(e),
            }

    def get_overall_health(self) -> dict[str, Any]:
        """
        Get comprehensive system health summary.

        Returns:
            Dict with all health metrics and overall status
        """
        try:
            job_health = self.get_job_status(hours=24)
            api_health = self.get_api_health(hours=1)
            db_health = self.get_database_health()
            queue_health = self.get_queue_status()

            # Determine overall health
            statuses = [
                job_health["health_status"],
                api_health["health_status"],
                db_health["health_status"],
                queue_health["health_status"],
            ]

            if any(s == "unhealthy" for s in statuses):
                overall_status = "unhealthy"
            elif any(s == "degraded" for s in statuses):
                overall_status = "degraded"
            elif any(s == "unavailable" for s in statuses):
                overall_status = "degraded"
            else:
                overall_status = "healthy"

            return {
                "overall_status": overall_status,
                "timestamp": datetime.utcnow().isoformat(),
                "components": {
                    "background_jobs": job_health,
                    "api": api_health,
                    "database": db_health,
                    "queue": queue_health,
                },
            }

        except Exception as e:
            logger.error(f"Error getting overall health: {e}")
            raise
