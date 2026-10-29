"""Platform admin endpoints for onboarding and managing referral-program marketers."""

from typing import NoReturn
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from loguru import logger
from sqlalchemy.orm import Session

from app.api.deps import get_current_platform_admin, get_current_super_admin, get_db
from app.db.models.referral import Marketer, MarketerStatus, PayoutBatch
from app.schemas.platform_admin import PlatformAdminResponse
from app.schemas.referral import (
    MarketerBankUpdateRequest,
    MarketerCreateRequest,
    MarketerListResponse,
    MarketerResponse,
    MarkPaidRequest,
    PayoutBatchDetail,
    PayoutBatchListResponse,
)
from app.services.enhanced_audit_service import EnhancedAuditService
from app.services.referral import payout_queries, payouts
from app.services.referral.marketer_service import (
    MarketerCodeTakenError,
    MarketerEmailInUseError,
    MarketerError,
    MarketerNotFoundError,
    MarketerService,
)
from app.utils.http_rate_limiter import platform_admin_rate_limit

router = APIRouter()

# Writes (create / bank / suspend / reactivate) decide who is paid and to which account, so they
# require a SUPER_ADMIN; the read endpoints stay open to any platform admin.
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
        logger.error(
            f"Failed to write audit log for {action} on marketer {marketer.id}: "
            f"{type(exc).__name__}"
        )


@router.post(
    "/",
    response_model=MarketerResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(platform_admin_rate_limit)],
)
def create_marketer(
    request: MarketerCreateRequest,
    db: Session = Depends(get_db),
    current_admin: PlatformAdminResponse = Depends(get_current_super_admin),
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
    current_admin: PlatformAdminResponse = Depends(get_current_super_admin),
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
    current_admin: PlatformAdminResponse = Depends(get_current_super_admin),
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
    current_admin: PlatformAdminResponse = Depends(get_current_super_admin),
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


# --- payout batches -----------------------------------------------------------------------------
# Reads: any platform admin. Writes (approve / retry / mark-paid) move money, so they require a
# SUPER_ADMIN (an ordinary ADMIN gets 403). Nothing is ever sent unless a super admin approves the
# batch; the payout functions themselves guarantee a transfer is never sent twice (see
# app/services/referral/payouts.py).

_BATCH_ERROR_MESSAGES = {
    "batch_not_found": "Payout batch not found",
    "payout_item_not_found": "That marketer has no payout item in this batch",
    "batch_state_invalid": "The batch is not in a state that allows this action",
    "item_not_markable": "Only awaiting-manual or failed items can be marked paid",
    "transfer_unverified": (
        "The earlier transfer could not be confirmed as failed, so it was not marked paid"
    ),
    "bank_reference_in_use": "That bank reference is already recorded against another payment",
    "payout_state_invalid": "The request conflicts with the current state of the batch",
}


def _raise_for_payout(exc: payouts.PayoutError) -> NoReturn:
    """Map a payout domain error to a stable ``{code, message}`` body. Never echoes ``str(exc)``:
    the service messages carry ids and provider text that the caller has no business seeing."""
    if isinstance(exc, payouts.BatchNotFoundError | payouts.PayoutItemNotFoundError):
        http_status = status.HTTP_404_NOT_FOUND
    else:
        http_status = status.HTTP_409_CONFLICT
    code = exc.code if exc.code in _BATCH_ERROR_MESSAGES else "payout_state_invalid"
    detail: dict = {"code": code, "message": _BATCH_ERROR_MESSAGES[code]}
    if isinstance(exc, payouts.ItemNotMarkableError):
        detail["marketer_id"] = str(exc.marketer_id)
        detail["item_status"] = exc.item_status.value
    raise HTTPException(status_code=http_status, detail=detail) from exc


def _audit_batch(
    db: Session,
    admin: PlatformAdminResponse,
    action: str,
    batch_id: UUID,
    description: str,
    metadata: dict,
) -> None:
    """Audit a payout action. The batch id and counts only: never a bank reference or detail.
    Never fails the request; a failure is logged by exception class and ids."""
    try:
        EnhancedAuditService(db).log_action(
            action_type=action,
            performed_by_type="platform_admin",
            performed_by_id=admin.id,
            performed_by_name=admin.email,
            target_type="payout_batch",
            target_id=batch_id,
            description=description,
            severity="high",
            metadata=metadata,
        )
    except Exception as exc:
        if not db.is_active:  # a failed commit/flush leaves the session unusable until rolled back
            db.rollback()
        logger.error(
            f"Failed to write audit log for {action} on payout batch {batch_id} "
            f"(admin {admin.id}): {type(exc).__name__}"
        )


def _detail(db: Session, batch_id: UUID) -> PayoutBatchDetail:
    detail = payout_queries.get_batch_detail(db, batch_id)
    if detail is None:
        _raise_for_payout(payouts.BatchNotFoundError(str(batch_id)))
    return detail


def _counts_metadata(detail: PayoutBatchDetail) -> dict:
    return {
        "status": detail.status.value,
        "total_kobo": detail.total_kobo,
        "item_counts": detail.item_counts.model_dump(),
    }


@router.get(
    "/batches",
    response_model=PayoutBatchListResponse,
    dependencies=[Depends(platform_admin_rate_limit)],
)
def list_batches(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_admin: PlatformAdminResponse = Depends(get_current_platform_admin),
):
    """Payout batches, newest first."""
    return payout_queries.list_batches(db, limit, offset)


@router.get(
    "/batches/{batch_id:uuid}",
    response_model=PayoutBatchDetail,
    dependencies=[Depends(platform_admin_rate_limit)],
)
def get_batch(
    batch_id: UUID,
    db: Session = Depends(get_db),
    current_admin: PlatformAdminResponse = Depends(get_current_platform_admin),
):
    """A batch with its items (``bank_last4`` only) and, per marketer, the held rewards left out."""
    return _detail(db, batch_id)


@router.post(
    "/batches/{batch_id:uuid}/approve",
    response_model=PayoutBatchDetail,
    dependencies=[Depends(platform_admin_rate_limit)],
)
def approve_batch(
    batch_id: UUID,
    db: Session = Depends(get_db),
    current_admin: PlatformAdminResponse = Depends(get_current_super_admin),
):
    """Approve a draft batch and pay its items. Idempotent: a batch that is no longer ``draft``
    is returned unchanged (200) and nothing is sent.

    Items are paid inside this request, one provider call each, so this endpoint is meant for
    the small batches of v1 (a handful of marketers twice a month). A large batch would need a
    background worker that drains the ``pending`` items instead of blocking the request.
    """

    def audit_approval(batch: PayoutBatch) -> None:
        # Runs after the draft -> paying commit and BEFORE the first provider call, and only when
        # this request actually performed the approval (a repeat approval is not audited).
        detail = payout_queries.get_batch_detail(db, batch_id)
        _audit_batch(
            db,
            current_admin,
            "approve_payout_batch",
            batch_id,
            f"Approved payout batch {batch_id}",
            _counts_metadata(detail) if detail is not None else {},
        )

    try:
        payouts.approve_batch(db, batch_id, current_admin.id, after_approval=audit_approval)
    except payouts.PayoutError as exc:
        _raise_for_payout(exc)
    detail = _detail(db, batch_id)
    return detail


@router.post(
    "/batches/{batch_id:uuid}/retry",
    response_model=PayoutBatchDetail,
    dependencies=[Depends(platform_admin_rate_limit)],
)
def retry_batch(
    batch_id: UUID,
    db: Session = Depends(get_db),
    current_admin: PlatformAdminResponse = Depends(get_current_super_admin),
):
    """Re-drive the unfinished items of an approved batch. Each item is looked up at the
    provider first and only re-sent when the provider confirms no live transfer exists. Like
    approve, it runs in the request (small batches only)."""
    try:
        payouts.retry_batch(db, batch_id, current_admin.id)
    except payouts.PayoutError as exc:
        _raise_for_payout(exc)
    detail = _detail(db, batch_id)
    _audit_batch(
        db,
        current_admin,
        "retry_payout_batch",
        batch_id,
        f"Retried payout batch {batch_id}",
        _counts_metadata(detail),
    )
    return detail


@router.post(
    "/batches/{batch_id:uuid}/mark-paid",
    response_model=PayoutBatchDetail,
    dependencies=[Depends(platform_admin_rate_limit)],
)
def mark_batch_paid(
    batch_id: UUID,
    request: MarkPaidRequest,
    db: Session = Depends(get_db),
    current_admin: PlatformAdminResponse = Depends(get_current_super_admin),
):
    """Record payments made outside the system. All entries are applied together or none are.
    The audit entry carries the batch id and the number of items, never the bank references."""
    entries = [(entry.marketer_id, entry.bank_reference) for entry in request.items]
    try:
        payouts.mark_paid(db, batch_id, entries, current_admin.id)
    except payouts.PayoutError as exc:
        _raise_for_payout(exc)
    detail = _detail(db, batch_id)
    _audit_batch(
        db,
        current_admin,
        "mark_payout_batch_paid",
        batch_id,
        f"Recorded {len(entries)} manual payment(s) for payout batch {batch_id}",
        {**_counts_metadata(detail), "items": len(entries)},
    )
    return detail
