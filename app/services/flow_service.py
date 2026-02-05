"""Flow service for managing conversational workflows."""

from typing import Optional, Tuple, List
from uuid import UUID

from sqlalchemy.orm import Session
from sqlalchemy import select, desc, func

from app.db.models.flow import Flow, FlowStatus, FlowTriggerType
from app.db.models.tenant import TenantCapabilities
from app.schemas.flow import (
    FlowCreate,
    FlowUpdate,
    FlowResponse,
)


class FlowService:
    """Service for flow management and validation."""

    def __init__(self, db: Session):
        """Initialize flow service with database session."""
        self.db = db

    def create_flow(self, flow_data: FlowCreate) -> FlowResponse:
        """
        Create a new flow with validation.

        Args:
            flow_data: Flow creation data

        Returns:
            Created flow response

        Raises:
            ValueError: If validation fails or limits exceeded
        """
        # Check tier limits
        self._check_tier_limits(flow_data.tenant_id)

        # Validate node structure
        if flow_data.nodes:
            self._validate_nodes(flow_data.nodes)

        # Check for webhook nodes if webhooks not allowed
        if flow_data.nodes:
            self._validate_webhook_usage(flow_data.tenant_id, flow_data.nodes)

        # Create flow
        db_flow = Flow(
            tenant_id=flow_data.tenant_id,
            name=flow_data.name,
            description=flow_data.description,
            status=flow_data.status,
            is_active=flow_data.is_active,
            trigger_type=flow_data.trigger_type,
            trigger_config=flow_data.trigger_config or {},
            nodes=flow_data.nodes or {},
            priority=flow_data.priority,
            version=flow_data.version,
            created_by=flow_data.created_by,
        )

        self.db.add(db_flow)
        self.db.commit()
        self.db.refresh(db_flow)

        return FlowResponse.model_validate(db_flow)

    def get_flow(self, flow_id: UUID) -> Optional[FlowResponse]:
        """
        Get flow by ID.

        Args:
            flow_id: Flow UUID

        Returns:
            Flow response if found, None otherwise
        """
        stmt = select(Flow).where(Flow.id == flow_id)
        flow = self.db.execute(stmt).scalar_one_or_none()

        if not flow:
            return None

        return FlowResponse.model_validate(flow)

    def get_tenant_flows(
        self,
        tenant_id: UUID,
        status: Optional[FlowStatus] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Tuple[List[FlowResponse], int]:
        """
        Get flows for a tenant with filtering and pagination.

        Args:
            tenant_id: Tenant UUID
            status: Optional status filter
            limit: Maximum number of flows to return
            offset: Number of flows to skip

        Returns:
            Tuple of (list of flow responses, total count)
        """
        # Build query
        stmt = select(Flow).where(Flow.tenant_id == tenant_id)

        if status:
            stmt = stmt.where(Flow.status == status)

        # Get total count
        count_stmt = select(func.count()).select_from(stmt.subquery())
        total = self.db.execute(count_stmt).scalar()

        # Apply ordering, limit, and offset
        stmt = stmt.order_by(desc(Flow.priority), desc(Flow.created_at)).limit(limit).offset(offset)

        flows = self.db.execute(stmt).scalars().all()

        return [FlowResponse.model_validate(f) for f in flows], total

    def update_flow(self, flow_id: UUID, flow_data: FlowUpdate) -> FlowResponse:
        """
        Update flow information.

        Args:
            flow_id: Flow UUID
            flow_data: Updated flow data

        Returns:
            Updated flow response

        Raises:
            ValueError: If flow not found or validation fails
        """
        stmt = select(Flow).where(Flow.id == flow_id)
        flow = self.db.execute(stmt).scalar_one_or_none()

        if not flow:
            raise ValueError("Flow not found")

        # Validate nodes if being updated
        update_data = flow_data.model_dump(exclude_unset=True)

        if "nodes" in update_data and update_data["nodes"]:
            self._validate_nodes(update_data["nodes"])
            # Check webhook usage when updating nodes
            self._validate_webhook_usage(flow.tenant_id, update_data["nodes"])

        # Update fields
        for field, value in update_data.items():
            setattr(flow, field, value)

        self.db.commit()
        self.db.refresh(flow)

        return FlowResponse.model_validate(flow)

    def delete_flow(self, flow_id: UUID) -> bool:
        """
        Delete a flow (soft delete by setting status to archived).

        Args:
            flow_id: Flow UUID

        Returns:
            True if deleted successfully

        Raises:
            ValueError: If flow not found
        """
        stmt = select(Flow).where(Flow.id == flow_id)
        flow = self.db.execute(stmt).scalar_one_or_none()

        if not flow:
            raise ValueError("Flow not found")

        flow.status = FlowStatus.ARCHIVED
        flow.is_active = False
        self.db.commit()

        return True

    def activate_flow(self, flow_id: UUID) -> FlowResponse:
        """
        Activate a flow.

        Args:
            flow_id: Flow UUID

        Returns:
            Updated flow response

        Raises:
            ValueError: If flow not found
        """
        stmt = select(Flow).where(Flow.id == flow_id)
        flow = self.db.execute(stmt).scalar_one_or_none()

        if not flow:
            raise ValueError("Flow not found")

        # Validate before activating
        if flow.nodes:
            self._validate_nodes(flow.nodes)

        flow.status = FlowStatus.ACTIVE
        flow.is_active = True

        self.db.commit()
        self.db.refresh(flow)

        return FlowResponse.model_validate(flow)

    def deactivate_flow(self, flow_id: UUID) -> FlowResponse:
        """
        Deactivate a flow.

        Args:
            flow_id: Flow UUID

        Returns:
            Updated flow response

        Raises:
            ValueError: If flow not found
        """
        stmt = select(Flow).where(Flow.id == flow_id)
        flow = self.db.execute(stmt).scalar_one_or_none()

        if not flow:
            raise ValueError("Flow not found")

        flow.status = FlowStatus.PAUSED
        flow.is_active = False

        self.db.commit()
        self.db.refresh(flow)

        return FlowResponse.model_validate(flow)

    def _validate_nodes(self, nodes: dict) -> None:
        """
        Validate flow node structure.

        Args:
            nodes: Flow nodes configuration

        Raises:
            ValueError: If node structure is invalid
        """
        if not isinstance(nodes, dict):
            raise ValueError("Nodes must be a dictionary")

        node_list = nodes.get("nodes", [])
        edges = nodes.get("edges", [])
        start_node_id = nodes.get("start_node_id")

        # Must have at least one node
        if not node_list:
            raise ValueError("Flow must have at least one node")

        # Must have a start node
        if not start_node_id:
            raise ValueError("Flow must have a start_node_id")

        # Collect all node IDs
        node_ids = set()
        for node in node_list:
            if not isinstance(node, dict):
                raise ValueError("Each node must be a dictionary")

            node_id = node.get("id")
            if not node_id:
                raise ValueError("Each node must have an 'id' field")

            if node_id in node_ids:
                raise ValueError(f"Duplicate node ID: {node_id}")

            node_ids.add(node_id)

            # Validate node has a type
            node_type = node.get("type")
            if not node_type:
                raise ValueError(f"Node {node_id} must have a 'type' field")

        # Validate start node exists
        if start_node_id not in node_ids:
            raise ValueError(f"Start node '{start_node_id}' not found in nodes")

        # Validate edge references
        for edge in edges:
            if not isinstance(edge, dict):
                raise ValueError("Each edge must be a dictionary")

            source = edge.get("source")
            target = edge.get("target")

            if not source:
                raise ValueError("Each edge must have a 'source' field")
            if not target:
                raise ValueError("Each edge must have a 'target' field")

            if source not in node_ids:
                raise ValueError(f"Edge source '{source}' not found in nodes")
            if target not in node_ids:
                raise ValueError(f"Edge target '{target}' not found in nodes")

        # Validate next_node_id references in node configs
        for node in node_list:
            config = node.get("config", {})

            # Check next_node_id if present
            next_node_id = config.get("next_node_id")
            if next_node_id and next_node_id not in node_ids:
                raise ValueError(
                    f"Node {node.get('id')} references non-existent next_node_id: {next_node_id}"
                )

            # For question nodes, validate option targets
            if node.get("type") == "question":
                options = config.get("options", [])
                for option in options:
                    option_target = option.get("next_node_id")
                    if option_target and option_target not in node_ids:
                        raise ValueError(
                            f"Question node {node.get('id')} option references non-existent node: {option_target}"
                        )

            # For condition nodes, validate branch targets
            if node.get("type") == "condition":
                conditions = config.get("conditions", [])
                for condition in conditions:
                    condition_target = condition.get("next_node_id")
                    if condition_target and condition_target not in node_ids:
                        raise ValueError(
                            f"Condition node {node.get('id')} references non-existent node: {condition_target}"
                        )

                # Check default path
                default_node_id = config.get("default_node_id")
                if default_node_id and default_node_id not in node_ids:
                    raise ValueError(
                        f"Condition node {node.get('id')} references non-existent default_node_id: {default_node_id}"
                    )

    def _check_tier_limits(self, tenant_id: UUID) -> None:
        """
        Check if tenant can create more flows based on subscription tier.

        Args:
            tenant_id: Tenant UUID

        Raises:
            ValueError: If tenant has reached flow limits
        """
        # Get tenant capabilities
        stmt = select(TenantCapabilities).where(TenantCapabilities.tenant_id == tenant_id)
        capabilities = self.db.execute(stmt).scalar_one_or_none()

        if not capabilities:
            raise ValueError("Tenant capabilities not found")

        # Check if capabilities has max_flows attribute (for future-proofing)
        # If the attribute exists, check the limit
        max_flows = getattr(capabilities, "max_flows", None)

        if max_flows is not None:
            # Count existing active flows
            stmt = select(func.count()).select_from(Flow).where(
                Flow.tenant_id == tenant_id,
                Flow.status != FlowStatus.ARCHIVED,
            )
            flow_count = self.db.execute(stmt).scalar()

            if flow_count >= max_flows:
                raise ValueError(
                    f"Flow limit reached. Your plan allows {max_flows} flows. "
                    f"Please upgrade your subscription to create more flows."
                )

    def _validate_webhook_usage(self, tenant_id: UUID, nodes: dict) -> None:
        """
        Validate that tenant can use webhook nodes if present.

        Args:
            tenant_id: Tenant UUID
            nodes: Flow nodes configuration

        Raises:
            ValueError: If webhooks are not allowed but webhook nodes are present
        """
        # Check for webhook nodes
        node_list = nodes.get("nodes", [])
        has_webhook = any(node.get("type") == "webhook" for node in node_list)

        if not has_webhook:
            return

        # Get tenant capabilities
        stmt = select(TenantCapabilities).where(TenantCapabilities.tenant_id == tenant_id)
        capabilities = self.db.execute(stmt).scalar_one_or_none()

        if not capabilities:
            raise ValueError("Tenant capabilities not found")

        # Check if capabilities has can_use_webhooks attribute (for future-proofing)
        can_use_webhooks = getattr(capabilities, "can_use_webhooks", True)  # Default to True for backward compatibility

        if not can_use_webhooks:
            raise ValueError(
                "Webhook nodes are not available on your current plan. "
                "Please upgrade to use webhook integrations."
            )

        # Check max_nodes_per_flow if it exists
        max_nodes = getattr(capabilities, "max_nodes_per_flow", None)
        if max_nodes is not None:
            node_count = len(node_list)
            if node_count > max_nodes:
                raise ValueError(
                    f"Flow exceeds maximum nodes limit. Your plan allows {max_nodes} nodes per flow, "
                    f"but this flow has {node_count} nodes."
                )
