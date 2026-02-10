"""Flow management endpoints."""

from copy import deepcopy
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from loguru import logger
from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.api.deps import (
    get_current_tenant_admin,
    get_db,
    verify_subscription_active,
    verify_tenant_access,
)
from app.db.models.flow import Flow, FlowExecution, FlowExecutionStatus, FlowStatus, FlowTriggerType
from app.services.flow_service import FlowService
from app.schemas.flow import (
    FlowCreate,
    FlowDetailResponse,
    FlowExecutionResponse,
    FlowExecutionListResponse,
    FlowListResponse,
    FlowResponse,
    FlowUpdate,
)
from app.schemas.user import UserResponse

router = APIRouter()

RESERVED_SYSTEM_KEYWORDS = {"start"}
TIER_ORDER = {"trial": 0, "lite": 1, "professional": 2, "growth": 3, "enterprise": 4}


def _status_for_flow_value_error(message: str) -> int:
    """Map flow validation errors to appropriate HTTP status codes."""
    text = (message or "").lower()
    plan_keywords = [
        "flow limit",
        "plan allows",
        "upgrade your subscription",
        "not available on your current plan",
    ]
    if any(keyword in text for keyword in plan_keywords):
        return status.HTTP_402_PAYMENT_REQUIRED
    return status.HTTP_400_BAD_REQUEST


def _normalize_tier_name(tier: str | None) -> str:
    """Normalize tier aliases to canonical tier names."""
    if not tier:
        return "trial"
    normalized = str(tier).strip().lower()
    aliases = {
        "pro": "professional",
        "starter": "lite",
    }
    return aliases.get(normalized, normalized)


def _enforce_template_min_tier(tenant_tier: str | None, required_tier: str | None) -> None:
    """Ensure tenant tier satisfies template minimum tier requirement."""
    tenant_tier_normalized = _normalize_tier_name(tenant_tier)
    required_tier_normalized = _normalize_tier_name(required_tier)

    tenant_level = TIER_ORDER.get(tenant_tier_normalized)
    required_level = TIER_ORDER.get(required_tier_normalized)

    if tenant_level is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid tenant subscription tier: {tenant_tier_normalized}",
        )
    if required_level is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid template minimum tier: {required_tier_normalized}",
        )
    if tenant_level < required_level:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=(
                f"This template requires {required_tier_normalized} tier or higher. "
                f"Your current tier is {tenant_tier_normalized}. Please upgrade your subscription."
            ),
        )


def _validate_reserved_trigger_keywords(trigger_type: FlowTriggerType, trigger_config: dict) -> None:
    """Ensure tenant flows don't use system-reserved trigger words."""
    if trigger_type != FlowTriggerType.KEYWORD:
        return

    keywords = trigger_config.get("keywords", []) if isinstance(trigger_config, dict) else []
    normalized = {str(k).strip().lower() for k in keywords if str(k).strip()}
    collisions = sorted(normalized.intersection(RESERVED_SYSTEM_KEYWORDS))
    if collisions:
        joined = ", ".join(collisions)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Reserved system keyword(s) not allowed in flow triggers: {joined}",
        )


def _normalize_template_nodes(template_nodes: dict | None) -> dict:
    """Normalize legacy template node schema to current flow-engine schema."""
    if not isinstance(template_nodes, dict):
        return {"start_node_id": "", "nodes": [], "edges": []}

    normalized = {
        "start_node_id": template_nodes.get("start_node_id", ""),
        "nodes": [],
        "edges": [],
    }
    raw_edges = template_nodes.get("edges", [])
    if isinstance(raw_edges, list):
        for edge in raw_edges:
            if not isinstance(edge, dict):
                continue
            source = edge.get("source") or edge.get("from")
            target = edge.get("target") or edge.get("to")
            if source and target:
                normalized["edges"].append({"source": source, "target": target})
    nodes = template_nodes.get("nodes", [])
    if not isinstance(nodes, list):
        return normalized

    for node in nodes:
        if not isinstance(node, dict):
            continue
        node_copy = dict(node)
        config = dict(node_copy.get("config", {}) or {})
        node_type = node_copy.get("type")

        # Legacy text input/question variable key
        if "save_to" not in config and "variable" in config:
            config["save_to"] = config["variable"]
            config.pop("variable", None)

        # Legacy rag_query key
        if node_type == "rag_query" and "save_to" not in config and "result_variable" in config:
            config["save_to"] = config["result_variable"]
            config.pop("result_variable", None)

        # QUESTION compatibility: if options omit next_node_id but node has next_node_id,
        # expand next_node_id to each option.
        if node_type == "question":
            options = config.get("options", [])
            fallback_next = config.get("next_node_id")
            if isinstance(options, list):
                updated_options = []
                for option in options:
                    if isinstance(option, dict):
                        option_copy = dict(option)
                        if fallback_next and not option_copy.get("next_node_id"):
                            option_copy["next_node_id"] = fallback_next
                        updated_options.append(option_copy)
                    else:
                        updated_options.append(option)
                config["options"] = updated_options

        node_copy["config"] = config
        normalized["nodes"].append(node_copy)

    return normalized


def _derive_trigger_type(trigger_config: dict | None) -> FlowTriggerType:
    """Infer trigger type from trigger config payload."""
    if not isinstance(trigger_config, dict):
        return FlowTriggerType.MANUAL
    if trigger_config.get("keywords"):
        return FlowTriggerType.KEYWORD
    if trigger_config.get("intents"):
        return FlowTriggerType.INTENT
    if trigger_config.get("schedule"):
        return FlowTriggerType.SCHEDULE
    if trigger_config.get("events"):
        return FlowTriggerType.EVENT
    return FlowTriggerType.MANUAL


def _normalize_template_trigger_config(
    template_name: str, template_description: str | None, trigger_config: dict | None
) -> dict:
    """Normalize trigger config for router-friendly flow templates."""
    normalized = dict(trigger_config or {})
    keywords = normalized.get("keywords", [])
    if isinstance(keywords, list):
        normalized["keywords"] = [k for k in keywords if str(k).strip().lower() != "start"]
    normalized.setdefault("match_mode", "word")
    normalized.setdefault("menu_label", template_name)
    normalized.setdefault("menu_description", template_description or "")
    normalized.setdefault("exclude_from_router", False)
    return normalized


def _refine_template_nodes(template_name: str, template_nodes: dict) -> dict:
    """Apply product-specific refinements to template node structures."""
    refined = deepcopy(template_nodes)
    nodes = refined.get("nodes", [])
    if not isinstance(nodes, list):
        return refined

    node_map = {node.get("id"): node for node in nodes if isinstance(node, dict)}

    if template_name == "FAQ Handler" and isinstance(node_map.get("provide_answer"), dict):
        node_map["provide_answer"]["type"] = "ai_action"
        node_map["provide_answer"]["config"] = {
            "prompt": "Answer the customer's question using retrieved knowledge. Question: {{user_question}}",
            "use_rag": True,
            "rag_key": "answer",
            "save_to": "faq_ai_answer",
            "next_node_id": "satisfaction_check",
            "max_tokens": 350,
            "max_history_turns": 8,
        }

    if template_name == "Billing Inquiry" and isinstance(node_map.get("provide_info"), dict):
        node_map["provide_info"]["type"] = "ai_action"
        node_map["provide_info"]["config"] = {
            "prompt": "Provide a concise billing answer using retrieved context. Issue: {{inquiry_type}}",
            "use_rag": True,
            "rag_key": "billing_info",
            "save_to": "billing_ai_answer",
            "next_node_id": "resolved_check",
            "max_tokens": 350,
            "max_history_turns": 8,
        }

    if template_name == "Upgrade Inquiry":
        if isinstance(node_map.get("show_plans"), dict):
            node_map["show_plans"]["type"] = "ai_action"
            node_map["show_plans"]["config"] = {
                "prompt": (
                    "Summarize the best plan recommendation for this customer based on "
                    "{{current_plan}}, {{upgrade_reason}}, {{team_size}}."
                ),
                "use_rag": True,
                "rag_key": "plan_details",
                "save_to": "upgrade_ai_summary",
                "next_node_id": "interest_level",
                "max_tokens": 350,
                "max_history_turns": 8,
            }
        if isinstance(node_map.get("create_lead"), dict):
            node_map["create_lead"]["config"]["next_node_id"] = "confirmation"
        if isinstance(node_map.get("confirmation"), dict):
            node_map["confirmation"]["config"]["next_node_id"] = "end"

    if template_name == "Bug Report":
        if isinstance(node_map.get("confirmation"), dict):
            node_map["confirmation"]["config"]["next_node_id"] = "check_critical"
        if "check_critical" not in node_map:
            nodes.append(
                {
                    "id": "check_critical",
                    "type": "condition",
                    "config": {
                        "conditions": [
                            {
                                "variable": "severity",
                                "operator": "equals",
                                "value": "critical",
                                "next_node_id": "urgent_escalate",
                            },
                            {
                                "variable": "severity",
                                "operator": "not_equals",
                                "value": "critical",
                                "next_node_id": "end",
                            },
                        ],
                        "default_node_id": "end",
                    },
                }
            )
            refined["nodes"] = nodes

    return refined


def _prune_unreachable_template_nodes(template_nodes: dict) -> dict:
    """Remove nodes not reachable from start_node_id via config-based routing."""
    pruned = deepcopy(template_nodes)
    nodes = pruned.get("nodes", [])
    start_node_id = pruned.get("start_node_id")
    if not isinstance(nodes, list) or not start_node_id:
        return pruned

    node_map = {node.get("id"): node for node in nodes if isinstance(node, dict) and node.get("id")}
    if start_node_id not in node_map:
        return pruned

    adjacency: dict[str, set[str]] = {}
    for node_id, node in node_map.items():
        config = node.get("config", {}) or {}
        targets: set[str] = set()

        next_node_id = config.get("next_node_id")
        if isinstance(next_node_id, str) and next_node_id:
            targets.add(next_node_id)

        default_node_id = config.get("default_node_id")
        if isinstance(default_node_id, str) and default_node_id:
            targets.add(default_node_id)

        options = config.get("options", [])
        if isinstance(options, list):
            for option in options:
                if isinstance(option, dict):
                    target = option.get("next_node_id")
                    if isinstance(target, str) and target:
                        targets.add(target)

        conditions = config.get("conditions", [])
        if isinstance(conditions, list):
            for condition in conditions:
                if isinstance(condition, dict):
                    target = condition.get("next_node_id")
                    if isinstance(target, str) and target:
                        targets.add(target)

        adjacency[node_id] = targets

    visited: set[str] = set()
    queue: list[str] = [start_node_id]
    while queue:
        current = queue.pop(0)
        if current in visited:
            continue
        visited.add(current)
        for nxt in adjacency.get(current, set()):
            if nxt not in visited and nxt in node_map:
                queue.append(nxt)

    pruned["nodes"] = [node for node in nodes if isinstance(node, dict) and node.get("id") in visited]
    return pruned


@router.post("", response_model=FlowResponse, status_code=status.HTTP_201_CREATED)
def create_flow(
    flow_data: FlowCreate,
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_tenant_admin),
    _subscription_check: object = Depends(verify_subscription_active),
):
    """
    Create a new flow.

    This endpoint allows admins to create automated conversation workflows
    that can be triggered by keywords, intents, or other conditions.

    Args:
        flow_data: Flow creation data
        db: Database session
        current_user: Current admin user

    Returns:
        Created flow

    Raises:
        HTTPException 403: If user doesn't have access to the tenant
        HTTPException 400: If flow data is invalid
    """
    # Verify tenant access
    verify_tenant_access(flow_data.tenant_id, current_user)

    try:
        _validate_reserved_trigger_keywords(flow_data.trigger_type, flow_data.trigger_config)

        flow_service = FlowService(db)
        flow_payload = flow_data.model_copy(update={"created_by": current_user.id})
        new_flow = flow_service.create_flow(flow_payload)

        logger.info(
            f"Flow created: {new_flow.name} (ID: {new_flow.id}) by user {current_user.email}"
        )

        return new_flow

    except HTTPException:
        raise
    except ValueError as e:
        db.rollback()
        logger.error(f"Failed to create flow: {e}")
        raise HTTPException(
            status_code=_status_for_flow_value_error(str(e)),
            detail=str(e),
        ) from e
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to create flow: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"Failed to create flow: {str(e)}"
        ) from e


@router.get("", response_model=FlowListResponse, status_code=status.HTTP_200_OK)
def list_flows(
    tenant_id: UUID | None = Query(None, description="Filter by tenant"),
    status_filter: FlowStatus | None = Query(None, description="Filter by status"),
    is_active: bool | None = Query(None, description="Filter by active state"),
    limit: int = Query(100, ge=1, le=500, description="Maximum flows to return"),
    offset: int = Query(0, ge=0, description="Number of flows to skip"),
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_tenant_admin),
):
    """
    List flows with pagination and filters.

    Args:
        tenant_id: Optional tenant filter
        status_filter: Optional status filter
        is_active: Optional active state filter
        limit: Maximum flows to return
        offset: Number of flows to skip
        db: Database session
        current_user: Current admin user

    Returns:
        Paginated list of flows

    Raises:
        HTTPException 403: If user doesn't have access to the tenant
    """
    # Determine tenant_id from current user
    if not tenant_id:
        tenant_id = current_user.tenant_id

    # Verify tenant access
    verify_tenant_access(tenant_id, current_user)

    # Build query with filters
    query = select(Flow).where(Flow.tenant_id == tenant_id)

    if status_filter is not None:
        query = query.where(Flow.status == status_filter)

    if is_active is not None:
        query = query.where(Flow.is_active == is_active)

    # Order by priority (descending) and creation date (descending)
    query = query.order_by(Flow.priority.desc(), Flow.created_at.desc())

    # Get total count
    count_query = select(func.count()).select_from(Flow).where(Flow.tenant_id == tenant_id)
    if status_filter is not None:
        count_query = count_query.where(Flow.status == status_filter)
    if is_active is not None:
        count_query = count_query.where(Flow.is_active == is_active)

    total = db.execute(count_query).scalar_one()

    # Apply pagination
    query = query.limit(limit).offset(offset)

    # Execute query
    flows = db.execute(query).scalars().all()

    logger.info(
        f"Listed {len(flows)} flows for tenant {tenant_id} " f"(total: {total}, offset: {offset})"
    )

    flow_responses = [FlowResponse.model_validate(flow) for flow in flows]
    return FlowListResponse(
        flows=flow_responses,
        total=total,
        page=offset // limit + 1,
        page_size=limit,
    )


# ============================================================================
# TEMPLATE ENDPOINTS
# ============================================================================


@router.get(
    "/templates",
    status_code=status.HTTP_200_OK,
)
def list_flow_templates(
    category: str | None = Query(None, description="Filter by category"),
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_tenant_admin),
):
    """
    List available flow templates.

    Returns pre-built flow templates that users can use as starting points
    for creating their own flows.

    Args:
        category: Optional category filter (sales, support, engagement)
        db: Database session
        current_user: Current user

    Returns:
        List of flow templates
    """
    from app.db.models.flow import FlowTemplate
    from app.db.models.tenant import Tenant

    try:
        # Build query
        stmt = select(FlowTemplate).where(FlowTemplate.is_active.is_(True))

        # Filter by category if provided
        if category:
            stmt = stmt.where(FlowTemplate.category == category)

        # Order by category, then name
        stmt = stmt.order_by(FlowTemplate.category, FlowTemplate.name)

        templates = db.execute(stmt).scalars().all()

        # Convert to response format
        template_list = []
        for template in templates:
            try:
                normalized_nodes = _prune_unreachable_template_nodes(
                    _refine_template_nodes(
                        template.name, _normalize_template_nodes(template.template_nodes)
                    )
                )
                normalized_trigger_config = _normalize_template_trigger_config(
                    template.name, template.description, template.trigger_config
                )
                normalized_trigger_type = _derive_trigger_type(normalized_trigger_config).value

                # Safely extract node types/count from template_nodes.nodes[]
                node_types: list[str] = []
                node_count = 0
                nodes = normalized_nodes.get("nodes", [])
                if isinstance(nodes, list):
                    node_types = list(
                        {
                            node.get("type", "unknown")
                            for node in nodes
                            if isinstance(node, dict)
                        }
                    )
                    node_count = len(nodes)

                template_list.append(
                    {
                        "id": str(template.id),
                        "name": template.name,
                        "description": template.description,
                        "category": template.category,
                        "min_tier": template.min_tier,
                        "preview": {
                            "trigger_type": normalized_trigger_type,
                            "node_count": node_count,
                            "node_types": node_types,
                        },
                    }
                )
            except Exception as e:
                logger.error(f"Error processing template {template.name}: {e}")
                continue

        logger.info(f"Retrieved {len(template_list)} flow templates")

        return {
            "templates": template_list,
            "total": len(template_list),
        }

    except Exception as e:
        logger.error(f"Failed to list flow templates: {e}")
        import traceback

        logger.error(f"Traceback: {traceback.format_exc()}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list templates: {str(e)}",
        ) from e


@router.get(
    "/templates/{template_id}",
    status_code=status.HTTP_200_OK,
)
def get_flow_template(
    template_id: UUID,
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_tenant_admin),
):
    """
    Get detailed flow template.

    Returns the full template definition that can be used to create a flow.

    Args:
        template_id: Template UUID
        db: Database session
        current_user: Current user

    Returns:
        Complete template details with nodes
    """
    from app.db.models.flow import FlowTemplate

    try:
        stmt = select(FlowTemplate).where(
            and_(FlowTemplate.id == template_id, FlowTemplate.is_active.is_(True))
        )
        template = db.execute(stmt).scalar_one_or_none()

        if not template:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Template not found")

        return {
            "id": str(template.id),
            "name": template.name,
            "description": template.description,
            "category": template.category,
            "min_tier": template.min_tier,
            "trigger_config": _normalize_template_trigger_config(
                template.name, template.description, template.trigger_config
            ),
            "nodes": _prune_unreachable_template_nodes(
                _refine_template_nodes(
                    template.name, _normalize_template_nodes(template.template_nodes)
                )
            ),
            "usage_instructions": {
                "steps": [
                    "Click 'Use Template' to create a flow from this template",
                    "Customize the messages and node configurations",
                    "Set your trigger keywords or conditions",
                    "Activate the flow when ready",
                ],
                "tips": [
                    "Test your flow before activating it",
                    "You can modify the template to fit your needs",
                    "Webhook nodes require valid API endpoints",
                ],
            },
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get template {template_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get template: {str(e)}",
        ) from e


@router.post(
    "/templates/{template_id}/use",
    response_model=FlowResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_flow_from_template(
    template_id: UUID,
    tenant_id: UUID = Query(..., description="Tenant to create flow for"),
    name: str | None = Query(None, description="Custom name for the flow"),
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_tenant_admin),
    _subscription_check: object = Depends(verify_subscription_active),
):
    """
    Create a new flow from a template.

    This endpoint creates a new flow based on a pre-built template,
    allowing users to quickly set up common workflows.

    Args:
        template_id: Template UUID
        tenant_id: Tenant to create flow for
        name: Optional custom name (defaults to template name)
        db: Database session
        current_user: Current user

    Returns:
        Created flow

    Raises:
        HTTPException 404: If template not found
        HTTPException 403: If user doesn't have access to the tenant
        HTTPException 400: If template tier exceeds user's plan
    """
    from app.db.models.flow import FlowTemplate

    # Verify tenant access
    verify_tenant_access(tenant_id, current_user)

    try:
        # Get template
        stmt = select(FlowTemplate).where(
            and_(FlowTemplate.id == template_id, FlowTemplate.is_active.is_(True))
        )
        template = db.execute(stmt).scalar_one_or_none()

        if not template:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Template not found")

        # Enforce minimum tier required for this template
        tenant_stmt = select(Tenant).where(Tenant.id == tenant_id)
        tenant = db.execute(tenant_stmt).scalar_one_or_none()
        if not tenant:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
        _enforce_template_min_tier(tenant.subscription_tier, template.min_tier)

        # Enforce tenant flow limits before creating a new flow from template
        flow_service = FlowService(db)
        flow_service._check_tier_limits(tenant_id)

        # Create flow from template
        normalized_trigger_config = _normalize_template_trigger_config(
            template.name, template.description, template.trigger_config
        )
        new_flow = Flow(
            tenant_id=tenant_id,
            name=name or f"{template.name} (Copy)",
            description=template.description,
            status=FlowStatus.DRAFT,  # Start as draft
            is_active=False,  # Not active by default
            trigger_type=_derive_trigger_type(normalized_trigger_config),
            trigger_config=normalized_trigger_config,
            nodes=_prune_unreachable_template_nodes(
                _refine_template_nodes(
                    template.name, _normalize_template_nodes(template.template_nodes)
                )
            ),
            priority=100,
            version=1,
            created_by=current_user.id,
        )

        db.add(new_flow)
        db.commit()
        db.refresh(new_flow)

        logger.info(
            f"Flow created from template {template.name} (ID: {new_flow.id}) "
            f"by user {current_user.email}"
        )

        return new_flow

    except HTTPException:
        raise
    except ValueError as e:
        db.rollback()
        logger.error(f"Failed to create flow from template: {e}")
        raise HTTPException(
            status_code=_status_for_flow_value_error(str(e)),
            detail=str(e),
        ) from e
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to create flow from template: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to create flow from template: {str(e)}",
        ) from e


@router.get("/{flow_id}", response_model=FlowDetailResponse, status_code=status.HTTP_200_OK)
def get_flow(
    flow_id: UUID,
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_tenant_admin),
):
    """
    Get flow by ID with execution statistics.

    Args:
        flow_id: Flow UUID
        db: Database session
        current_user: Current admin user

    Returns:
        Flow details with execution statistics

    Raises:
        HTTPException 404: If flow not found
        HTTPException 403: If user doesn't have access to the flow's tenant
    """
    # Get flow
    stmt = select(Flow).where(Flow.id == flow_id)
    flow = db.execute(stmt).scalar_one_or_none()

    if not flow:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Flow not found")

    # Verify tenant access
    verify_tenant_access(flow.tenant_id, current_user)

    # Get execution statistics
    exec_count_stmt = (
        select(func.count()).select_from(FlowExecution).where(FlowExecution.flow_id == flow_id)
    )
    execution_count = db.execute(exec_count_stmt).scalar_one()

    last_exec_stmt = (
        select(FlowExecution.started_at)
        .where(FlowExecution.flow_id == flow_id)
        .order_by(FlowExecution.started_at.desc())
        .limit(1)
    )
    last_executed = db.execute(last_exec_stmt).scalar_one_or_none()

    # Build response with statistics
    flow_dict = {
        "id": flow.id,
        "tenant_id": flow.tenant_id,
        "name": flow.name,
        "description": flow.description,
        "status": flow.status,
        "is_active": flow.is_active,
        "trigger_type": flow.trigger_type,
        "trigger_config": flow.trigger_config,
        "nodes": flow.nodes,
        "priority": flow.priority,
        "version": flow.version,
        "created_by": flow.created_by,
        "created_at": flow.created_at,
        "updated_at": flow.updated_at,
        "execution_count": execution_count,
        "last_executed_at": last_executed,
    }

    logger.info(f"Retrieved flow: {flow.name} (ID: {flow_id}) with {execution_count} executions")

    return FlowDetailResponse.model_validate(flow_dict)


@router.put("/{flow_id}", response_model=FlowResponse, status_code=status.HTTP_200_OK)
def update_flow(
    flow_id: UUID,
    flow_data: FlowUpdate,
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_tenant_admin),
    _subscription_check: object = Depends(verify_subscription_active),
):
    """
    Update flow information.

    Args:
        flow_id: Flow UUID
        flow_data: Updated flow data
        db: Database session
        current_user: Current admin user

    Returns:
        Updated flow

    Raises:
        HTTPException 404: If flow not found
        HTTPException 403: If user doesn't have access to the flow's tenant
        HTTPException 400: If update data is invalid
    """
    # Get flow
    stmt = select(Flow).where(Flow.id == flow_id)
    flow = db.execute(stmt).scalar_one_or_none()

    if not flow:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Flow not found")

    # Verify tenant access
    verify_tenant_access(flow.tenant_id, current_user)

    try:
        flow_service = FlowService(db)
        # Update fields
        update_data = flow_data.model_dump(exclude_unset=True)
        merged_trigger_type = update_data.get("trigger_type", flow.trigger_type)
        merged_trigger_config = update_data.get("trigger_config", flow.trigger_config)
        _validate_reserved_trigger_keywords(merged_trigger_type, merged_trigger_config)

        activating_via_update = (
            update_data.get("status") == FlowStatus.ACTIVE or update_data.get("is_active") is True
        )
        if flow.status == FlowStatus.ARCHIVED and activating_via_update:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot activate archived flow via update. Use restore flow workflow first.",
            )

        if "nodes" in update_data and update_data["nodes"]:
            update_data["nodes"] = flow_service._normalize_nodes_payload(update_data["nodes"])
            flow_service._validate_nodes(update_data["nodes"])
            flow_service._validate_webhook_usage(flow.tenant_id, update_data["nodes"])

        for field, value in update_data.items():
            setattr(flow, field, value)

        db.commit()
        db.refresh(flow)

        logger.info(f"Flow updated: {flow.name} (ID: {flow_id}) by user {current_user.email}")

        return flow

    except Exception as e:
        db.rollback()
        logger.error(f"Failed to update flow {flow_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"Failed to update flow: {str(e)}"
        ) from e


@router.delete("/{flow_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_flow(
    flow_id: UUID,
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_tenant_admin),
):
    """
    Delete (archive) a flow.

    This soft-deletes the flow by setting its status to ARCHIVED
    and is_active to False.

    Args:
        flow_id: Flow UUID
        db: Database session
        current_user: Current admin user

    Returns:
        No content

    Raises:
        HTTPException 404: If flow not found
        HTTPException 403: If user doesn't have access to the flow's tenant
    """
    # Get flow
    stmt = select(Flow).where(Flow.id == flow_id)
    flow = db.execute(stmt).scalar_one_or_none()

    if not flow:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Flow not found")

    # Verify tenant access
    verify_tenant_access(flow.tenant_id, current_user)

    try:
        # Soft delete: archive and deactivate
        flow.status = FlowStatus.ARCHIVED
        flow.is_active = False

        db.commit()

        logger.info(f"Flow archived: {flow.name} (ID: {flow_id}) by user {current_user.email}")

        return None

    except Exception as e:
        db.rollback()
        logger.error(f"Failed to delete flow {flow_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"Failed to delete flow: {str(e)}"
        ) from e


@router.post("/{flow_id}/activate", response_model=FlowResponse, status_code=status.HTTP_200_OK)
def activate_flow(
    flow_id: UUID,
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_tenant_admin),
    _subscription_check: object = Depends(verify_subscription_active),
):
    """
    Activate a flow.

    Sets the flow status to ACTIVE and is_active to True.

    Args:
        flow_id: Flow UUID
        db: Database session
        current_user: Current admin user

    Returns:
        Updated flow

    Raises:
        HTTPException 404: If flow not found
        HTTPException 403: If user doesn't have access to the flow's tenant
        HTTPException 400: If flow is archived
    """
    # Get flow
    stmt = select(Flow).where(Flow.id == flow_id)
    flow = db.execute(stmt).scalar_one_or_none()

    if not flow:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Flow not found")

    # Verify tenant access
    verify_tenant_access(flow.tenant_id, current_user)

    # Cannot activate archived flows
    if flow.status == FlowStatus.ARCHIVED:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot activate archived flow. Please restore it first.",
        )

    try:
        flow.status = FlowStatus.ACTIVE
        flow.is_active = True

        db.commit()
        db.refresh(flow)

        logger.info(f"Flow activated: {flow.name} (ID: {flow_id}) by user {current_user.email}")

        return flow

    except Exception as e:
        db.rollback()
        logger.error(f"Failed to activate flow {flow_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"Failed to activate flow: {str(e)}"
        ) from e


@router.post("/{flow_id}/deactivate", response_model=FlowResponse, status_code=status.HTTP_200_OK)
def deactivate_flow(
    flow_id: UUID,
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_tenant_admin),
):
    """
    Deactivate a flow.

    Sets the flow status to PAUSED and is_active to False.

    Args:
        flow_id: Flow UUID
        db: Database session
        current_user: Current admin user

    Returns:
        Updated flow

    Raises:
        HTTPException 404: If flow not found
        HTTPException 403: If user doesn't have access to the flow's tenant
    """
    # Get flow
    stmt = select(Flow).where(Flow.id == flow_id)
    flow = db.execute(stmt).scalar_one_or_none()

    if not flow:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Flow not found")

    # Verify tenant access
    verify_tenant_access(flow.tenant_id, current_user)

    try:
        flow.status = FlowStatus.PAUSED
        flow.is_active = False

        db.commit()
        db.refresh(flow)

        logger.info(f"Flow deactivated: {flow.name} (ID: {flow_id}) by user {current_user.email}")

        return flow

    except Exception as e:
        db.rollback()
        logger.error(f"Failed to deactivate flow {flow_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"Failed to deactivate flow: {str(e)}"
        ) from e


@router.get(
    "/{flow_id}/executions",
    response_model=FlowExecutionListResponse,
    status_code=status.HTTP_200_OK,
)
def get_flow_executions(
    flow_id: UUID,
    status_filter: FlowExecutionStatus | None = Query(None, description="Filter by status"),
    limit: int = Query(100, ge=1, le=500, description="Maximum executions to return"),
    offset: int = Query(0, ge=0, description="Number of executions to skip"),
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_tenant_admin),
):
    """
    Get flow execution history.

    Args:
        flow_id: Flow UUID
        status_filter: Optional status filter
        limit: Maximum executions to return
        offset: Number of executions to skip
        db: Database session
        current_user: Current admin user

    Returns:
        Paginated list of flow executions

    Raises:
        HTTPException 404: If flow not found
        HTTPException 403: If user doesn't have access to the flow's tenant
    """
    # Get flow to verify access
    stmt = select(Flow).where(Flow.id == flow_id)
    flow = db.execute(stmt).scalar_one_or_none()

    if not flow:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Flow not found")

    # Verify tenant access
    verify_tenant_access(flow.tenant_id, current_user)

    # Build query with filters
    query = select(FlowExecution).where(FlowExecution.flow_id == flow_id)

    if status_filter is not None:
        query = query.where(FlowExecution.status == status_filter)

    # Order by creation date (descending)
    query = query.order_by(FlowExecution.created_at.desc())

    # Get total count
    count_query = (
        select(func.count()).select_from(FlowExecution).where(FlowExecution.flow_id == flow_id)
    )
    if status_filter is not None:
        count_query = count_query.where(FlowExecution.status == status_filter)

    total = db.execute(count_query).scalar_one()

    # Apply pagination
    query = query.limit(limit).offset(offset)

    # Execute query
    executions = db.execute(query).scalars().all()

    logger.info(
        f"Listed {len(executions)} executions for flow {flow_id} "
        f"(total: {total}, offset: {offset})"
    )

    execution_responses = [FlowExecutionResponse.model_validate(execution) for execution in executions]
    return FlowExecutionListResponse(
        executions=execution_responses,
        total=total,
        page=offset // limit + 1,
        page_size=limit,
    )


@router.get(
    "/{flow_id}/analytics",
    status_code=status.HTTP_200_OK,
)
def get_flow_analytics(
    flow_id: UUID,
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_tenant_admin),
):
    """
    Get flow analytics and statistics.

    Returns metrics like total executions, success rate, average completion time, etc.

    Args:
        flow_id: Flow UUID
        db: Database session
        current_user: Current admin user

    Returns:
        Flow analytics data

    Raises:
        HTTPException 404: If flow not found
        HTTPException 403: If user doesn't have access to the flow's tenant
    """
    # Get flow to verify access
    stmt = select(Flow).where(Flow.id == flow_id)
    flow = db.execute(stmt).scalar_one_or_none()

    if not flow:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Flow not found")

    # Verify tenant access
    verify_tenant_access(flow.tenant_id, current_user)

    # Calculate analytics
    try:
        # Total executions
        total_executions_stmt = (
            select(func.count()).select_from(FlowExecution).where(FlowExecution.flow_id == flow_id)
        )
        total_executions = db.execute(total_executions_stmt).scalar_one()

        # Executions by status
        status_counts = {}
        for exec_status in FlowExecutionStatus:
            count_stmt = (
                select(func.count())
                .select_from(FlowExecution)
                .where(
                    and_(
                        FlowExecution.flow_id == flow_id,
                        FlowExecution.status == exec_status,
                    )
                )
            )
            status_counts[exec_status.value] = db.execute(count_stmt).scalar_one()

        # Success rate
        completed_count = status_counts.get(FlowExecutionStatus.COMPLETED.value, 0)
        success_rate = (completed_count / total_executions * 100) if total_executions > 0 else 0

        # Average completion time (for completed executions)
        avg_time_stmt = select(
            func.avg(func.extract("epoch", FlowExecution.completed_at - FlowExecution.started_at))
        ).where(
            and_(
                FlowExecution.flow_id == flow_id,
                FlowExecution.status == FlowExecutionStatus.COMPLETED,
                FlowExecution.started_at.isnot(None),
                FlowExecution.completed_at.isnot(None),
            )
        )
        avg_completion_seconds = db.execute(avg_time_stmt).scalar_one()

        # Last execution
        last_exec_stmt = (
            select(FlowExecution)
            .where(FlowExecution.flow_id == flow_id)
            .order_by(FlowExecution.created_at.desc())
            .limit(1)
        )
        last_execution = db.execute(last_exec_stmt).scalar_one_or_none()

        analytics = {
            "flow_id": str(flow_id),
            "flow_name": flow.name,
            "total_executions": total_executions,
            "status_breakdown": status_counts,
            "success_rate_percentage": round(success_rate, 2),
            "average_completion_time_seconds": (
                round(avg_completion_seconds, 2) if avg_completion_seconds else None
            ),
            "last_execution": (
                {
                    "id": str(last_execution.id) if last_execution else None,
                    "status": last_execution.status.value if last_execution else None,
                    "created_at": last_execution.created_at.isoformat() if last_execution else None,
                    "completed_at": (
                        last_execution.completed_at.isoformat()
                        if last_execution and last_execution.completed_at
                        else None
                    ),
                }
                if last_execution
                else None
            ),
        }

        logger.info(
            f"Retrieved analytics for flow {flow_id}: "
            f"{total_executions} executions, {success_rate:.1f}% success rate"
        )

        return analytics

    except Exception as e:
        logger.error(f"Failed to calculate analytics for flow {flow_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to calculate analytics: {str(e)}",
        ) from e
