"""Tenant automation endpoints (actions and execution audit)."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_tenant_admin, get_db, verify_subscription_active
from app.db.models.automation import ActionTriggerSource
from app.schemas.automation import (
    ActionExecutionApprovalRequest,
    ActionExecutionResponse,
    ActionSecretCreate,
    ActionSecretResponse,
    ActionTestRequest,
    TenantActionCreate,
    TenantActionResponse,
    TenantActionUpdate,
)
from app.schemas.user import UserResponse
from app.services.automation_service import AutomationService

router = APIRouter()


@router.post(
    "/actions",
    response_model=TenantActionResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_action(
    payload: TenantActionCreate,
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_tenant_admin),
    _subscription_check: object = Depends(verify_subscription_active),
):
    service = AutomationService(db)
    try:
        action = service.create_action(
            tenant_id=current_user.tenant_id,
            user_id=current_user.id,
            payload=payload,
        )
        return TenantActionResponse.model_validate(action)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e)) from e


@router.get(
    "/actions",
    response_model=list[TenantActionResponse],
    status_code=status.HTTP_200_OK,
)
def list_actions(
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_tenant_admin),
):
    service = AutomationService(db)
    actions = service.list_actions(tenant_id=current_user.tenant_id)
    return [TenantActionResponse.model_validate(a) for a in actions]


@router.get(
    "/actions/{action_id}",
    response_model=TenantActionResponse,
    status_code=status.HTTP_200_OK,
)
def get_action(
    action_id: UUID,
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_tenant_admin),
):
    service = AutomationService(db)
    action = service.get_action(tenant_id=current_user.tenant_id, action_id=action_id)
    if not action:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Action not found")
    return TenantActionResponse.model_validate(action)


@router.patch(
    "/actions/{action_id}",
    response_model=TenantActionResponse,
    status_code=status.HTTP_200_OK,
)
def update_action(
    action_id: UUID,
    payload: TenantActionUpdate,
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_tenant_admin),
):
    service = AutomationService(db)
    action = service.update_action(
        tenant_id=current_user.tenant_id,
        action_id=action_id,
        user_id=current_user.id,
        payload=payload,
    )
    if not action:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Action not found")
    return TenantActionResponse.model_validate(action)


@router.delete(
    "/actions/{action_id}",
    status_code=status.HTTP_200_OK,
)
def delete_action(
    action_id: UUID,
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_tenant_admin),
):
    service = AutomationService(db)
    deleted = service.delete_action(tenant_id=current_user.tenant_id, action_id=action_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Action not found")
    return {"message": "Action deleted successfully"}


@router.post(
    "/actions/{action_id}/test",
    response_model=ActionExecutionResponse,
    status_code=status.HTTP_200_OK,
)
async def test_action(
    action_id: UUID,
    payload: ActionTestRequest,
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_tenant_admin),
):
    service = AutomationService(db)
    action = service.get_action(tenant_id=current_user.tenant_id, action_id=action_id)
    if not action:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Action not found")

    execution = await service.execute_action(
        tenant_id=current_user.tenant_id,
        action=action,
        input_payload=payload.input_payload,
        trigger_source=ActionTriggerSource.MANUAL_TEST,
    )
    return ActionExecutionResponse.model_validate(execution)


@router.get(
    "/executions",
    response_model=list[ActionExecutionResponse],
    status_code=status.HTTP_200_OK,
)
def list_executions(
    action_id: UUID | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_tenant_admin),
):
    service = AutomationService(db)
    rows = service.list_executions(
        tenant_id=current_user.tenant_id,
        action_id=action_id,
        limit=limit,
    )
    return [ActionExecutionResponse.model_validate(r) for r in rows]


@router.get(
    "/executions/{execution_id}",
    response_model=ActionExecutionResponse,
    status_code=status.HTTP_200_OK,
)
def get_execution(
    execution_id: UUID,
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_tenant_admin),
):
    service = AutomationService(db)
    row = service.get_execution(tenant_id=current_user.tenant_id, execution_id=execution_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Execution not found")
    return ActionExecutionResponse.model_validate(row)


@router.post(
    "/executions/{execution_id}/retry",
    response_model=ActionExecutionResponse,
    status_code=status.HTTP_200_OK,
)
async def retry_execution(
    execution_id: UUID,
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_tenant_admin),
):
    service = AutomationService(db)
    row = await service.retry_execution(tenant_id=current_user.tenant_id, execution_id=execution_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Execution not found")
    return ActionExecutionResponse.model_validate(row)


@router.post(
    "/executions/{execution_id}/approval",
    response_model=ActionExecutionResponse,
    status_code=status.HTTP_200_OK,
)
async def set_execution_approval(
    execution_id: UUID,
    payload: ActionExecutionApprovalRequest,
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_tenant_admin),
):
    service = AutomationService(db)
    execution = service.get_execution(tenant_id=current_user.tenant_id, execution_id=execution_id)
    if not execution:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Execution not found")

    if not execution.requires_approval:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Execution does not require approval",
        )

    if execution.approval_status != "pending":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Execution approval has already been resolved",
        )

    row = await service.set_execution_approval(
        tenant_id=current_user.tenant_id,
        execution_id=execution_id,
        approved=payload.approved,
        approved_by=current_user.id,
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Execution not found")
    return ActionExecutionResponse.model_validate(row)


@router.post(
    "/secrets",
    response_model=ActionSecretResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_secret(
    payload: ActionSecretCreate,
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_tenant_admin),
):
    service = AutomationService(db)
    secret = service.create_secret(
        tenant_id=current_user.tenant_id,
        provider=payload.provider,
        key_name=payload.key_name,
        secret_value=payload.secret_value,
    )
    return ActionSecretResponse.model_validate(secret)


@router.get(
    "/secrets",
    response_model=list[ActionSecretResponse],
    status_code=status.HTTP_200_OK,
)
def list_secrets(
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_tenant_admin),
):
    service = AutomationService(db)
    rows = service.list_secrets(tenant_id=current_user.tenant_id)
    return [ActionSecretResponse.model_validate(row) for row in rows]


@router.delete(
    "/secrets/{secret_id}",
    status_code=status.HTTP_200_OK,
)
def delete_secret(
    secret_id: UUID,
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_tenant_admin),
):
    service = AutomationService(db)
    deleted = service.delete_secret(tenant_id=current_user.tenant_id, secret_id=secret_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Secret not found")
    return {"message": "Secret deleted successfully"}
