"""Flow service for managing conversational workflows."""

from uuid import UUID

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from app.db.models.automation import TenantAction
from app.db.models.flow import Flow, FlowStatus
from app.db.models.subscription_plan import SubscriptionPlan
from app.db.models.tenant import Tenant, TenantCapabilities
from app.schemas.flow import FlowCreate, FlowResponse, FlowUpdate


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

        normalized_nodes = (
            self._normalize_nodes_payload(flow_data.nodes) if flow_data.nodes else flow_data.nodes
        )

        # Validate node structure
        if normalized_nodes:
            self._validate_nodes(normalized_nodes)

        # Check for webhook nodes if webhooks not allowed
        if normalized_nodes:
            self._validate_webhook_usage(flow_data.tenant_id, normalized_nodes)
            self._validate_action_call_usage(flow_data.tenant_id, normalized_nodes)

        # Create flow
        db_flow = Flow(
            tenant_id=flow_data.tenant_id,
            name=flow_data.name,
            description=flow_data.description,
            status=flow_data.status,
            is_active=flow_data.is_active,
            trigger_type=flow_data.trigger_type,
            trigger_config=flow_data.trigger_config or {},
            nodes=normalized_nodes or {},
            priority=flow_data.priority,
            version=flow_data.version,
            created_by=flow_data.created_by,
        )

        self.db.add(db_flow)
        self.db.commit()
        self.db.refresh(db_flow)

        return FlowResponse.model_validate(db_flow)

    def get_flow(self, flow_id: UUID) -> FlowResponse | None:
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
        status: FlowStatus | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[FlowResponse], int]:
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
        total = self.db.execute(count_stmt).scalar() or 0

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
            update_data["nodes"] = self._normalize_nodes_payload(update_data["nodes"])
            self._validate_nodes(update_data["nodes"])
            # Check webhook usage when updating nodes
            self._validate_webhook_usage(flow.tenant_id, update_data["nodes"])
            self._validate_action_call_usage(flow.tenant_id, update_data["nodes"])

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

            # Support both ReactFlow-style edges ("source"/"target")
            # and legacy builder edges ("from"/"to").
            source = edge.get("source") or edge.get("from")
            target = edge.get("target") or edge.get("to")

            if not source:
                raise ValueError("Each edge must have a 'source' (or legacy 'from') field")
            if not target:
                raise ValueError("Each edge must have a 'target' (or legacy 'to') field")

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

        # Validate next_node_id exists for non-terminal nodes (prevents loop bugs)
        terminal_node_types = {"end", "escalate"}

        for node in node_list:
            node_id = node.get("id")
            node_type = node.get("type")
            config = node.get("config", {})

            # Skip terminal nodes (they don't need next_node_id)
            if node_type in terminal_node_types:
                continue

            # Validate QUESTION nodes
            if node_type == "question":
                options = config.get("options", [])

                # Check if question has config-level next_node_id
                has_config_next = "next_node_id" in config

                # Check if ALL options have next_node_id
                all_options_have_next = all("next_node_id" in option for option in options)

                # Question must have EITHER:
                # 1. Config-level next_node_id (all options go to same node), OR
                # 2. All options have their own next_node_id
                if not has_config_next and not all_options_have_next:
                    raise ValueError(
                        f"QUESTION node '{node_id}' must have either:\n"
                        f"  - 'next_node_id' in config (all options go to same node), OR\n"
                        f"  - 'next_node_id' in EVERY option (conditional branching)\n"
                        f"Without this, the flow will loop indefinitely on this node."
                    )

            # Validate CONDITION nodes
            elif node_type == "condition":
                # Condition nodes must have default_node_id
                if "default_node_id" not in config:
                    raise ValueError(
                        f"CONDITION node '{node_id}' must have 'default_node_id' in config"
                    )

                # All conditions must have next_node_id
                conditions = config.get("conditions", [])
                for idx, condition in enumerate(conditions):
                    if "next_node_id" not in condition:
                        raise ValueError(
                            f"CONDITION node '{node_id}' condition #{idx} must have 'next_node_id'"
                        )

            # Validate linear flow nodes (MESSAGE, TEXT_INPUT, RAG_QUERY, WEBHOOK, etc.)
            else:
                if "next_node_id" not in config:
                    raise ValueError(
                        f"{node_type.upper()} node '{node_id}' must have 'next_node_id' in config.\n"
                        f"Without this, the flow will loop indefinitely on this node.\n"
                        f"Tip: Set 'next_node_id' to the ID of the next node in the flow."
                    )

    def _normalize_nodes_payload(self, nodes: dict) -> dict:
        """Normalize flow nodes payload to canonical edge shape (source/target)."""
        if not isinstance(nodes, dict):
            return nodes

        normalized = dict(nodes)
        edges = nodes.get("edges", [])
        if isinstance(edges, list):
            normalized_edges = []
            for edge in edges:
                if not isinstance(edge, dict):
                    normalized_edges.append(edge)
                    continue

                source = edge.get("source") or edge.get("from")
                target = edge.get("target") or edge.get("to")
                edge_copy = dict(edge)
                if source is not None:
                    edge_copy["source"] = source
                if target is not None:
                    edge_copy["target"] = target
                edge_copy.pop("from", None)
                edge_copy.pop("to", None)
                normalized_edges.append(edge_copy)
            normalized["edges"] = normalized_edges

        return normalized

    def _check_tier_limits(self, tenant_id: UUID) -> None:
        """
        Check if tenant can create more flows based on subscription tier.

        Args:
            tenant_id: Tenant UUID

        Raises:
            ValueError: If tenant has reached flow limits
        """
        limits = self._get_flow_limits(tenant_id)
        max_flows = limits.get("max_flows")

        if max_flows is not None:
            # Count existing active flows
            count_stmt = (
                select(func.count())
                .select_from(Flow)
                .where(
                    Flow.tenant_id == tenant_id,
                    Flow.status != FlowStatus.ARCHIVED,
                )
            )
            flow_count = self.db.execute(count_stmt).scalar()

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

        limits = self._get_flow_limits(tenant_id)
        can_use_webhooks = limits.get("can_use_webhooks", True)

        if not can_use_webhooks:
            raise ValueError(
                "Webhook nodes are not available on your current plan. "
                "Please upgrade to use webhook integrations."
            )

        # Check max_nodes_per_flow if it exists
        max_nodes = limits.get("max_nodes_per_flow")
        if max_nodes is not None:
            node_count = len(node_list)
            if node_count > max_nodes:
                raise ValueError(
                    f"Flow exceeds maximum nodes limit. Your plan allows {max_nodes} nodes per flow, "
                    f"but this flow has {node_count} nodes."
                )

    def _validate_action_call_usage(self, tenant_id: UUID, nodes: dict) -> None:
        """
        Validate action_call node references and tenant scoping.

        Args:
            tenant_id: Tenant UUID
            nodes: Flow nodes configuration

        Raises:
            ValueError: If action reference is missing/invalid.
        """
        node_list = nodes.get("nodes", [])
        action_call_nodes = [node for node in node_list if node.get("type") == "action_call"]
        if not action_call_nodes:
            return

        for node in action_call_nodes:
            node_id = node.get("id")
            config = node.get("config", {})
            action_id = config.get("action_id")
            if not action_id:
                raise ValueError(f"ACTION_CALL node '{node_id}' must include 'action_id'.")

            action = self.db.execute(
                select(TenantAction).where(
                    TenantAction.id == action_id,
                    TenantAction.tenant_id == tenant_id,
                )
            ).scalar_one_or_none()
            if not action:
                raise ValueError(
                    f"ACTION_CALL node '{node_id}' references unknown action_id: {action_id}"
                )

    def _get_flow_limits(self, tenant_id: UUID) -> dict:
        """
        Resolve flow-related limits from subscription plan, with capability override support.

        Returns:
            Dict containing max_flows, max_nodes_per_flow, can_use_webhooks
        """
        tenant = self.db.execute(select(Tenant).where(Tenant.id == tenant_id)).scalar_one_or_none()
        if not tenant:
            raise ValueError("Tenant not found")

        plan = self.db.execute(
            select(SubscriptionPlan).where(
                SubscriptionPlan.tier == tenant.subscription_tier,
                SubscriptionPlan.is_active.is_(True),
            )
        ).scalar_one_or_none()
        if not plan:
            plan = self.db.execute(
                select(SubscriptionPlan).where(SubscriptionPlan.tier == tenant.subscription_tier)
            ).scalar_one_or_none()
        if not plan:
            raise ValueError(f"Subscription plan not found for tier: {tenant.subscription_tier}")

        limits = {
            "max_flows": plan.max_flows,
            "max_nodes_per_flow": plan.max_nodes_per_flow,
            "can_use_webhooks": plan.can_use_webhooks,
        }

        # Optional override path for deployments where these fields exist on capabilities.
        capabilities = self.db.execute(
            select(TenantCapabilities).where(TenantCapabilities.tenant_id == tenant_id)
        ).scalar_one_or_none()
        if capabilities:
            override_max_flows = getattr(capabilities, "max_flows", None)
            override_max_nodes = getattr(capabilities, "max_nodes_per_flow", None)
            override_webhooks = getattr(capabilities, "can_use_webhooks", None)
            if override_max_flows is not None:
                limits["max_flows"] = override_max_flows
            if override_max_nodes is not None:
                limits["max_nodes_per_flow"] = override_max_nodes
            if override_webhooks is not None:
                limits["can_use_webhooks"] = override_webhooks

        return limits
