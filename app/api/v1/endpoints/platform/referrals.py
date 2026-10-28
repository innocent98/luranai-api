"""Platform admin endpoints for onboarding and managing referral-program marketers."""

from typing import NoReturn
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from loguru import logger
from sqlalchemy.orm import Session

from app.api.deps import get_current_platform_admin, get_db
from app.db.models.referral import Marketer, MarketerStatus
from app.schemas.platform_admin import PlatformAdminResponse
from app.schemas.referral import (
    MarketerBankUpdateRequest,
    MarketerCreateRequest,
    MarketerListResponse,
    MarketerResponse,
)
from app.services.enhanced_audit_service import EnhancedAuditService
from app.services.referral.marketer_service import (
    MarketerCodeTakenError,
    MarketerEmailInUseError,
    MarketerError,
    MarketerNotFoundError,
    MarketerService,
)
from app.utils.http_rate_limiter import platform_admin_rate_limit

router = APIRouter()

# Id path params use the uuid convertor so literal sibling routes added later in this router
# (/batches, /rewards, /config) can never be captured as a marketer id.

_ERROR_MESSAGES = {
    "marketer_not_found": "Marketer not found",
    "code_taken": "That referral code is already in use",
    "email_in_use": "Email already in use",
}


def _raise_for(exc: Exception) -> NoReturn:
    if isinstance(exc, MarketerNotFoundError):
        http_status = status.HTTP_404_NOT_FOUND
    elif isinstance(exc, MarketerCodeTakenError | MarketerEmailInUseError):
        http_status = status.HTTP_409_CONFLICT
    elif isinstance(exc, MarketerError):
        http_status = status.HTTP_400_BAD_REQUEST
    else:  # ValueError from the service's own validation (the schema normally catches it first)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "invalid_input", "message": str(exc)},
        ) from exc
    raise HTTPException(
        status_code=http_status,
        detail={"code": exc.code, "message": _ERROR_MESSAGES.get(exc.code, str(exc))},
    ) from exc


def _audit(
    db: Session,
    admin: PlatformAdminResponse,
    action: str,
    marketer: Marketer,
    description: str,
) -> None:
    """Record the admin action. Never carries bank details, and never fails the request."""
    try:
        EnhancedAuditService(db).log_action(
            action_type=action,
            performed_by_type="platform_admin",
            performed_by_id=admin.id,
            performed_by_name=admin.email,
            target_type="marketer",
            target_id=marketer.id,
            description=description,
            severity="medium",
            metadata={"code": marketer.code},
        )
    except Exception as exc:
        db.rollback()
        logger.error(f"Failed to write audit log for {action} on marketer {marketer.id}: {exc}")


@router.post(
    "/",
    response_model=MarketerResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(platform_admin_rate_limit)],
)
def create_marketer(
    request: MarketerCreateRequest,
    db: Session = Depends(get_db),
    current_admin: PlatformAdminResponse = Depends(get_current_platform_admin),
):
    """Onboard a marketer and email them a one-time set-password link."""
    try:
        marketer = MarketerService(db).create(
            admin_id=current_admin.id,
            name=request.name,
            email=str(request.email),
            code=request.code,
            bank_code=request.bank_code,
            account_number=request.account_number,
            account_name=request.account_name,
        )
    except (MarketerError, ValueError) as exc:
        _raise_for(exc)
    _audit(db, current_admin, "create_marketer", marketer, f"Created marketer {marketer.code}")
    return MarketerResponse.from_marketer(marketer)


@router.get(
    "/",
    response_model=MarketerListResponse,
    dependencies=[Depends(platform_admin_rate_limit)],
)
def list_marketers(
    status_filter: MarketerStatus | None = Query(None, alias="status"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_admin: PlatformAdminResponse = Depends(get_current_platform_admin),
):
    marketers, total = MarketerService(db).list(status=status_filter, limit=limit, offset=offset)
    return MarketerListResponse(
        items=[MarketerResponse.from_marketer(m) for m in marketers],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/{marketer_id:uuid}",
    response_model=MarketerResponse,
    dependencies=[Depends(platform_admin_rate_limit)],
)
def get_marketer(
    marketer_id: UUID,
    db: Session = Depends(get_db),
    current_admin: PlatformAdminResponse = Depends(get_current_platform_admin),
):
    try:
        marketer = MarketerService(db).get(marketer_id)
    except MarketerError as exc:
        _raise_for(exc)
    return MarketerResponse.from_marketer(marketer)


@router.patch(
    "/{marketer_id:uuid}/bank",
    response_model=MarketerResponse,
    dependencies=[Depends(platform_admin_rate_limit)],
)
def update_marketer_bank(
    marketer_id: UUID,
    request: MarketerBankUpdateRequest,
    db: Session = Depends(get_db),
    current_admin: PlatformAdminResponse = Depends(get_current_platform_admin),
):
    try:
        marketer = MarketerService(db).update_bank_details(
            marketer_id,
            bank_code=request.bank_code,
            account_number=request.account_number,
            account_name=request.account_name,
        )
    except (MarketerError, ValueError) as exc:
        _raise_for(exc)
    _audit(
        db,
        current_admin,
        "update_marketer_bank",
        marketer,
        f"Updated payout details for marketer {marketer.code}",
    )
    return MarketerResponse.from_marketer(marketer)


@router.post(
    "/{marketer_id:uuid}/suspend",
    response_model=MarketerResponse,
    dependencies=[Depends(platform_admin_rate_limit)],
)
def suspend_marketer(
    marketer_id: UUID,
    db: Session = Depends(get_db),
    current_admin: PlatformAdminResponse = Depends(get_current_platform_admin),
):
    """Suspend a marketer: the code stops attributing and unpaid rewards move to held."""
    try:
        marketer = MarketerService(db).suspend(marketer_id)
    except MarketerError as exc:
        _raise_for(exc)
    _audit(db, current_admin, "suspend_marketer", marketer, f"Suspended marketer {marketer.code}")
    return MarketerResponse.from_marketer(marketer)


@router.post(
    "/{marketer_id:uuid}/reactivate",
    response_model=MarketerResponse,
    dependencies=[Depends(platform_admin_rate_limit)],
)
def reactivate_marketer(
    marketer_id: UUID,
    db: Session = Depends(get_db),
    current_admin: PlatformAdminResponse = Depends(get_current_platform_admin),
):
    """Reactivate a marketer. Held rewards are not released automatically."""
    try:
        marketer = MarketerService(db).reactivate(marketer_id)
    except MarketerError as exc:
        _raise_for(exc)
    _audit(
        db, current_admin, "reactivate_marketer", marketer, f"Reactivated marketer {marketer.code}"
    )
    return MarketerResponse.from_marketer(marketer)
