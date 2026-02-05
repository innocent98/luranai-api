"""Flow management endpoints."""

from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.orm import Session
from sqlalchemy import select, func, and_
from loguru import logger

from app.api.deps import get_current_tenant_admin, get_db, verify_tenant_access
from app.db.models.flow import Flow, FlowExecution, FlowStatus, FlowExecutionStatus
from app.schemas.flow import (
    FlowCreate,
    FlowUpdate,
    FlowResponse,
    FlowDetailResponse,
    FlowListResponse,
    FlowExecutionResponse,
    FlowExecutionListResponse,
)
from app.schemas.user import UserResponse

router = APIRouter()


@router.post("", response_model=FlowResponse, status_code=status.HTTP_201_CREATED)
def create_flow(
    flow_data: FlowCreate,
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_tenant_admin),
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
        # Create new flow
        new_flow = Flow(
            tenant_id=flow_data.tenant_id,
            name=flow_data.name,
            description=flow_data.description,
            status=flow_data.status,
            is_active=flow_data.is_active,
            trigger_type=flow_data.trigger_type,
            trigger_config=flow_data.trigger_config,
            nodes=flow_data.nodes,
            priority=flow_data.priority,
            version=flow_data.version,
            created_by=current_user.id,
        )

        db.add(new_flow)
        db.commit()
        db.refresh(new_flow)

        logger.info(
            f"Flow created: {new_flow.name} (ID: {new_flow.id}) "
            f"by user {current_user.email}"
        )

        return new_flow

    except Exception as e:
        db.rollback()
        logger.error(f"Failed to create flow: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to create flow: {str(e)}"
        )


@router.get("", response_model=FlowListResponse, status_code=status.HTTP_200_OK)
def list_flows(
    tenant_id: Optional[UUID] = Query(None, description="Filter by tenant"),
    status_filter: Optional[FlowStatus] = Query(None, description="Filter by status"),
    is_active: Optional[bool] = Query(None, description="Filter by active state"),
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
        f"Listed {len(flows)} flows for tenant {tenant_id} "
        f"(total: {total}, offset: {offset})"
    )

    return FlowListResponse(
        flows=flows,
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
    category: Optional[str] = Query(None, description="Filter by category"),
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

    try:
        # Build query
        stmt = select(FlowTemplate).where(FlowTemplate.is_active == True)

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
                # Safely extract node types
                node_types = []
                if template.template_nodes and isinstance(template.template_nodes, dict):
                    node_types = list(set(node.get("type", "unknown") for node in template.template_nodes.values() if isinstance(node, dict)))

                # Safely extract trigger type
                trigger_type = None
                if isinstance(template.trigger_config, dict):
                    trigger_type = template.trigger_config.get("trigger_type")

                template_list.append({
                    "id": str(template.id),
                    "name": template.name,
                    "description": template.description,
                    "category": template.category,
                    "min_tier": template.min_tier,
                    "preview": {
                        "trigger_type": trigger_type,
                        "node_count": len(template.template_nodes) if template.template_nodes else 0,
                        "node_types": node_types,
                    },
                })
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
            detail=f"Failed to list templates: {str(e)}"
        )


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
            and_(
                FlowTemplate.id == template_id,
                FlowTemplate.is_active == True
            )
        )
        template = db.execute(stmt).scalar_one_or_none()

        if not template:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Template not found"
            )

        return {
            "id": str(template.id),
            "name": template.name,
            "description": template.description,
            "category": template.category,
            "min_tier": template.min_tier,
            "trigger_config": template.trigger_config,
            "nodes": template.template_nodes,
            "usage_instructions": {
                "steps": [
                    "Click 'Use Template' to create a flow from this template",
                    "Customize the messages and node configurations",
                    "Set your trigger keywords or conditions",
                    "Activate the flow when ready"
                ],
                "tips": [
                    "Test your flow before activating it",
                    "You can modify the template to fit your needs",
                    "Webhook nodes require valid API endpoints"
                ]
            }
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get template {template_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get template: {str(e)}"
        )


@router.post(
    "/templates/{template_id}/use",
    response_model=FlowResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_flow_from_template(
    template_id: UUID,
    tenant_id: UUID = Query(..., description="Tenant to create flow for"),
    name: Optional[str] = Query(None, description="Custom name for the flow"),
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_tenant_admin),
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
            and_(
                FlowTemplate.id == template_id,
                FlowTemplate.is_active == True
            )
        )
        template = db.execute(stmt).scalar_one_or_none()

        if not template:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Template not found"
            )

        # TODO: Check if tenant's plan supports this template (min_tier check)
        # For now, we'll allow all templates

        # Create flow from template
        new_flow = Flow(
            tenant_id=tenant_id,
            name=name or f"{template.name} (Copy)",
            description=template.description,
            status=FlowStatus.DRAFT,  # Start as draft
            is_active=False,  # Not active by default
            trigger_type=template.trigger_config.get("trigger_type", "keyword"),
            trigger_config=template.trigger_config,
            nodes=template.template_nodes,
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
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to create flow from template: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to create flow from template: {str(e)}"
        )
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
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Flow not found"
        )

    # Verify tenant access
    verify_tenant_access(flow.tenant_id, current_user)

    # Get execution statistics
    exec_count_stmt = select(func.count()).select_from(FlowExecution).where(
        FlowExecution.flow_id == flow_id
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

    logger.info(
        f"Retrieved flow: {flow.name} (ID: {flow_id}) with {execution_count} executions"
    )

    return FlowDetailResponse(**flow_dict)


@router.put("/{flow_id}", response_model=FlowResponse, status_code=status.HTTP_200_OK)
def update_flow(
    flow_id: UUID,
    flow_data: FlowUpdate,
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_tenant_admin),
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
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Flow not found"
        )

    # Verify tenant access
    verify_tenant_access(flow.tenant_id, current_user)

    try:
        # Update fields
        update_data = flow_data.model_dump(exclude_unset=True)

        for field, value in update_data.items():
            setattr(flow, field, value)

        db.commit()
        db.refresh(flow)

        logger.info(
            f"Flow updated: {flow.name} (ID: {flow_id}) by user {current_user.email}"
        )

        return flow

    except Exception as e:
        db.rollback()
        logger.error(f"Failed to update flow {flow_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to update flow: {str(e)}"
        )


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
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Flow not found"
        )

    # Verify tenant access
    verify_tenant_access(flow.tenant_id, current_user)

    try:
        # Soft delete: archive and deactivate
        flow.status = FlowStatus.ARCHIVED
        flow.is_active = False

        db.commit()

        logger.info(
            f"Flow archived: {flow.name} (ID: {flow_id}) by user {current_user.email}"
        )

        return None

    except Exception as e:
        db.rollback()
        logger.error(f"Failed to delete flow {flow_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to delete flow: {str(e)}"
        )


@router.post("/{flow_id}/activate", response_model=FlowResponse, status_code=status.HTTP_200_OK)
def activate_flow(
    flow_id: UUID,
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_tenant_admin),
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
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Flow not found"
        )

    # Verify tenant access
    verify_tenant_access(flow.tenant_id, current_user)

    # Cannot activate archived flows
    if flow.status == FlowStatus.ARCHIVED:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot activate archived flow. Please restore it first."
        )

    try:
        flow.status = FlowStatus.ACTIVE
        flow.is_active = True

        db.commit()
        db.refresh(flow)

        logger.info(
            f"Flow activated: {flow.name} (ID: {flow_id}) by user {current_user.email}"
        )

        return flow

    except Exception as e:
        db.rollback()
        logger.error(f"Failed to activate flow {flow_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to activate flow: {str(e)}"
        )


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
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Flow not found"
        )

    # Verify tenant access
    verify_tenant_access(flow.tenant_id, current_user)

    try:
        flow.status = FlowStatus.PAUSED
        flow.is_active = False

        db.commit()
        db.refresh(flow)

        logger.info(
            f"Flow deactivated: {flow.name} (ID: {flow_id}) by user {current_user.email}"
        )

        return flow

    except Exception as e:
        db.rollback()
        logger.error(f"Failed to deactivate flow {flow_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to deactivate flow: {str(e)}"
        )


@router.get(
    "/{flow_id}/executions",
    response_model=FlowExecutionListResponse,
    status_code=status.HTTP_200_OK,
)
def get_flow_executions(
    flow_id: UUID,
    status_filter: Optional[FlowExecutionStatus] = Query(None, description="Filter by status"),
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
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Flow not found"
        )

    # Verify tenant access
    verify_tenant_access(flow.tenant_id, current_user)

    # Build query with filters
    query = select(FlowExecution).where(FlowExecution.flow_id == flow_id)

    if status_filter is not None:
        query = query.where(FlowExecution.status == status_filter)

    # Order by creation date (descending)
    query = query.order_by(FlowExecution.created_at.desc())

    # Get total count
    count_query = select(func.count()).select_from(FlowExecution).where(
        FlowExecution.flow_id == flow_id
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

    return FlowExecutionListResponse(
        executions=executions,
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
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Flow not found"
        )

    # Verify tenant access
    verify_tenant_access(flow.tenant_id, current_user)

    # Calculate analytics
    try:
        # Total executions
        total_executions_stmt = select(func.count()).select_from(FlowExecution).where(
            FlowExecution.flow_id == flow_id
        )
        total_executions = db.execute(total_executions_stmt).scalar_one()

        # Executions by status
        status_counts = {}
        for exec_status in FlowExecutionStatus:
            count_stmt = select(func.count()).select_from(FlowExecution).where(
                and_(
                    FlowExecution.flow_id == flow_id,
                    FlowExecution.status == exec_status,
                )
            )
            status_counts[exec_status.value] = db.execute(count_stmt).scalar_one()

        # Success rate
        completed_count = status_counts.get(FlowExecutionStatus.COMPLETED.value, 0)
        success_rate = (
            (completed_count / total_executions * 100) if total_executions > 0 else 0
        )

        # Average completion time (for completed executions)
        avg_time_stmt = select(
            func.avg(
                func.extract('epoch', FlowExecution.completed_at - FlowExecution.started_at)
            )
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
            "last_execution": {
                "id": str(last_execution.id) if last_execution else None,
                "status": last_execution.status.value if last_execution else None,
                "created_at": last_execution.created_at.isoformat() if last_execution else None,
                "completed_at": (
                    last_execution.completed_at.isoformat()
                    if last_execution and last_execution.completed_at
                    else None
                ),
            } if last_execution else None,
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
            detail=f"Failed to calculate analytics: {str(e)}"
        )


