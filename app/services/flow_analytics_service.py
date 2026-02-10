"""Flow analytics service for calculating flow execution metrics."""

from typing import Any
from uuid import UUID

from sqlalchemy import extract, func
from sqlalchemy.orm import Session

from app.db.models.flow import Flow, FlowExecution, FlowExecutionStatus


class FlowAnalyticsService:
    """Service for calculating and retrieving flow execution analytics metrics."""

    def __init__(self, db: Session):
        """Initialize flow analytics service with database session."""
        self.db = db

    def get_flow_metrics(self, flow_id: UUID) -> dict[str, Any]:
        """
        Get execution metrics for a single flow.

        Args:
            flow_id: Flow UUID

        Returns:
            Dictionary containing flow metrics:
            - total_executions: Total number of executions
            - completed_executions: Number of successful completions
            - failed_executions: Number of failures
            - completion_rate: Percentage of successful completions
            - failure_rate: Percentage of failures
            - avg_completion_time_seconds: Average time to complete
            - executions_by_status: Breakdown by status
        """
        # Get total executions count
        total_executions = (
            self.db.query(func.count(FlowExecution.id))
            .filter(FlowExecution.flow_id == flow_id)
            .scalar()
            or 0
        )

        # Get status breakdown
        status_counts = (
            self.db.query(
                FlowExecution.status,
                func.count(FlowExecution.id).label("count"),
            )
            .filter(FlowExecution.flow_id == flow_id)
            .group_by(FlowExecution.status)
            .all()
        )

        executions_by_status = {status.value: count for status, count in status_counts}

        # Extract specific counts
        completed_executions = executions_by_status.get(FlowExecutionStatus.COMPLETED.value, 0)
        failed_executions = executions_by_status.get(FlowExecutionStatus.FAILED.value, 0)

        # Calculate rates (handle division by zero)
        completion_rate = (
            (completed_executions / total_executions * 100) if total_executions > 0 else 0.0
        )
        failure_rate = (failed_executions / total_executions * 100) if total_executions > 0 else 0.0

        # Calculate average completion time for completed executions
        avg_completion_time = (
            self.db.query(
                func.avg(extract("epoch", FlowExecution.completed_at - FlowExecution.started_at))
            )
            .filter(
                FlowExecution.flow_id == flow_id,
                FlowExecution.status == FlowExecutionStatus.COMPLETED,
                FlowExecution.started_at.isnot(None),
                FlowExecution.completed_at.isnot(None),
            )
            .scalar()
            or 0.0
        )

        return {
            "total_executions": total_executions,
            "completed_executions": completed_executions,
            "failed_executions": failed_executions,
            "completion_rate": round(completion_rate, 2),
            "failure_rate": round(failure_rate, 2),
            "avg_completion_time_seconds": round(float(avg_completion_time), 2),
            "executions_by_status": executions_by_status,
        }

    def get_tenant_flow_metrics(self, tenant_id: UUID) -> dict[str, Any]:
        """
        Get aggregate flow execution metrics for a tenant.

        Args:
            tenant_id: Tenant UUID

        Returns:
            Dictionary containing aggregate metrics across all tenant flows:
            - total_executions: Total executions across all flows
            - completed_executions: Total successful completions
            - failed_executions: Total failures
            - completion_rate: Overall completion percentage
            - failure_rate: Overall failure percentage
            - avg_completion_time_seconds: Average completion time across all flows
            - executions_by_status: Aggregate status breakdown
            - total_flows: Number of flows for this tenant
            - active_flows: Number of active flows
        """
        # Get total executions count for tenant flows
        total_executions = (
            self.db.query(func.count(FlowExecution.id))
            .join(Flow, FlowExecution.flow_id == Flow.id)
            .filter(Flow.tenant_id == tenant_id)
            .scalar()
            or 0
        )

        # Get status breakdown across all tenant flows
        status_counts = (
            self.db.query(
                FlowExecution.status,
                func.count(FlowExecution.id).label("count"),
            )
            .join(Flow, FlowExecution.flow_id == Flow.id)
            .filter(Flow.tenant_id == tenant_id)
            .group_by(FlowExecution.status)
            .all()
        )

        executions_by_status = {status.value: count for status, count in status_counts}

        # Extract specific counts
        completed_executions = executions_by_status.get(FlowExecutionStatus.COMPLETED.value, 0)
        failed_executions = executions_by_status.get(FlowExecutionStatus.FAILED.value, 0)

        # Calculate rates (handle division by zero)
        completion_rate = (
            (completed_executions / total_executions * 100) if total_executions > 0 else 0.0
        )
        failure_rate = (failed_executions / total_executions * 100) if total_executions > 0 else 0.0

        # Calculate average completion time across all tenant flows
        avg_completion_time = (
            self.db.query(
                func.avg(extract("epoch", FlowExecution.completed_at - FlowExecution.started_at))
            )
            .join(Flow, FlowExecution.flow_id == Flow.id)
            .filter(
                Flow.tenant_id == tenant_id,
                FlowExecution.status == FlowExecutionStatus.COMPLETED,
                FlowExecution.started_at.isnot(None),
                FlowExecution.completed_at.isnot(None),
            )
            .scalar()
            or 0.0
        )

        # Get flow counts
        total_flows = (
            self.db.query(func.count(Flow.id)).filter(Flow.tenant_id == tenant_id).scalar() or 0
        )

        active_flows = (
            self.db.query(func.count(Flow.id))
            .filter(Flow.tenant_id == tenant_id, Flow.is_active.is_(True))
            .scalar()
            or 0
        )

        return {
            "total_executions": total_executions,
            "completed_executions": completed_executions,
            "failed_executions": failed_executions,
            "completion_rate": round(completion_rate, 2),
            "failure_rate": round(failure_rate, 2),
            "avg_completion_time_seconds": round(float(avg_completion_time), 2),
            "executions_by_status": executions_by_status,
            "total_flows": total_flows,
            "active_flows": active_flows,
        }

    def get_flow_completion_funnel(self, flow_id: UUID) -> dict[str, Any]:
        """
        Track drop-off rates by node to identify where users abandon flows.

        Args:
            flow_id: Flow UUID

        Returns:
            Dictionary containing funnel analysis:
            - total_executions: Total executions started
            - drop_off_by_node: Dictionary mapping node_id to drop-off count
            - completion_rate: Overall completion percentage
            - most_common_exit_node: Node where most users drop off
        """
        # Get all executions for this flow
        executions = self.db.query(FlowExecution).filter(FlowExecution.flow_id == flow_id).all()

        total_executions = len(executions)

        if total_executions == 0:
            return {
                "total_executions": 0,
                "drop_off_by_node": {},
                "completion_rate": 0.0,
                "most_common_exit_node": None,
            }

        # Track drop-offs by current node for non-completed executions
        drop_off_by_node: dict[str, int] = {}
        completed_count = 0

        for execution in executions:
            if execution.status == FlowExecutionStatus.COMPLETED:
                completed_count += 1
            elif execution.current_node_id:
                # This is where the user dropped off
                node_id = execution.current_node_id
                drop_off_by_node[node_id] = drop_off_by_node.get(node_id, 0) + 1

        completion_rate = (
            (completed_count / total_executions * 100) if total_executions > 0 else 0.0
        )

        # Find the most common exit node
        most_common_exit_node = None
        if drop_off_by_node:
            most_common_exit_node = max(drop_off_by_node.items(), key=lambda x: x[1])[0]

        return {
            "total_executions": total_executions,
            "drop_off_by_node": drop_off_by_node,
            "completion_rate": round(completion_rate, 2),
            "most_common_exit_node": most_common_exit_node,
        }

    def get_execution_time_stats(self, flow_id: UUID) -> dict[str, Any]:
        """
        Get detailed execution time statistics for a flow.

        Args:
            flow_id: Flow UUID

        Returns:
            Dictionary containing time statistics:
            - avg_completion_time_seconds: Average time to complete
            - min_completion_time_seconds: Fastest completion
            - max_completion_time_seconds: Slowest completion
            - median_completion_time_seconds: Median completion time
            - total_completed: Number of completed executions analyzed
        """
        # Get completion times for completed executions
        completion_times = (
            self.db.query(
                extract("epoch", FlowExecution.completed_at - FlowExecution.started_at).label(
                    "duration"
                )
            )
            .filter(
                FlowExecution.flow_id == flow_id,
                FlowExecution.status == FlowExecutionStatus.COMPLETED,
                FlowExecution.started_at.isnot(None),
                FlowExecution.completed_at.isnot(None),
            )
            .all()
        )

        if not completion_times:
            return {
                "avg_completion_time_seconds": 0.0,
                "min_completion_time_seconds": 0.0,
                "max_completion_time_seconds": 0.0,
                "median_completion_time_seconds": 0.0,
                "total_completed": 0,
            }

        # Extract durations
        durations = [float(t[0]) for t in completion_times if t[0] is not None]

        if not durations:
            return {
                "avg_completion_time_seconds": 0.0,
                "min_completion_time_seconds": 0.0,
                "max_completion_time_seconds": 0.0,
                "median_completion_time_seconds": 0.0,
                "total_completed": 0,
            }

        # Calculate statistics
        avg_time = sum(durations) / len(durations)
        min_time = min(durations)
        max_time = max(durations)

        # Calculate median
        sorted_durations = sorted(durations)
        n = len(sorted_durations)
        if n % 2 == 0:
            median_time = (sorted_durations[n // 2 - 1] + sorted_durations[n // 2]) / 2
        else:
            median_time = sorted_durations[n // 2]

        return {
            "avg_completion_time_seconds": round(avg_time, 2),
            "min_completion_time_seconds": round(min_time, 2),
            "max_completion_time_seconds": round(max_time, 2),
            "median_completion_time_seconds": round(median_time, 2),
            "total_completed": len(durations),
        }
