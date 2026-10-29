"""Platform admin endpoints for onboarding and managing referral-program marketers."""

from datetime import UTC, datetime
from typing import Literal, NoReturn
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from loguru import logger
from sqlalchemy.orm import Session

from app.api.deps import get_current_platform_admin, get_current_super_admin, get_db
from app.db.models.referral import Marketer, MarketerStatus, PayoutBatch, RewardStatus
from app.schemas.platform_admin import PlatformAdminResponse
from app.schemas.referral import (
    MarketerBankUpdateRequest,
    MarketerCreateRequest,
    MarketerListResponse,
    MarketerResponse,
    MarkPaidRequest,
    PayoutBatchDetail,
    PayoutBatchListResponse,
    PrepareBatchResponse,
    ReferralConfigResponse,
    ReferralConfigUpdate,
    RewardActionRequest,
    RewardActionResponse,
    RewardQueueResponse,
)
from app.services.enhanced_audit_service import EnhancedAuditService
from app.services.referral import payout_queries, payouts, review_queue, rewards
from app.services.referral.config import (
    CONFIG_KEYS,
    get_config_sources,
    get_referral_config,
    update_referral_config,
)
from app.services.referral.marketer_service import (
    MarketerAuditError,
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


_FAILED_CODES = {
    "create_marketer": "marketer_create_failed",
    "update_marketer_bank": "bank_update_failed",
    "suspend_marketer": "marketer_suspend_failed",
    "reactivate_marketer": "marketer_reactivate_failed",
}


def _audit_hook(db: Session, admin: PlatformAdminResponse, action: str, description: str):
    """The audit write for a marketer change, handed to ``MarketerService``.

    It runs inside the service, which flushes the change first: ``log_action`` commits, so the
    change and its record become durable in ONE commit (and a failure rolls the change back, see
    ``MarketerService._commit_audited``). Never carries bank details.
    """

    def write(marketer: Marketer) -> None:
        EnhancedAuditService(db).log_action(
            action_type=action,
            performed_by_type="platform_admin",
            performed_by_id=admin.id,
            performed_by_name=admin.email,
            target_type="marketer",
            target_id=marketer.id,
            description=f"{description} {marketer.code}",
            severity="medium",
            metadata={"code": marketer.code},
        )

    return write


def _raise_audit_failed(action: str) -> NoReturn:
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail={"code": _FAILED_CODES[action], "message": "The change was not saved"},
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
            audit=_audit_hook(db, current_admin, "create_marketer", "Created marketer"),
        )
    except MarketerAuditError:
        _raise_audit_failed("create_marketer")
    except (MarketerError, ValueError) as exc:
        _raise_for(exc)
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
            audit=_audit_hook(
                db, current_admin, "update_marketer_bank", "Updated payout details for marketer"
            ),
        )
    except MarketerAuditError:
        _raise_audit_failed("update_marketer_bank")
    except (MarketerError, ValueError) as exc:
        _raise_for(exc)
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
        marketer = MarketerService(db).suspend(
            marketer_id,
            audit=_audit_hook(db, current_admin, "suspend_marketer", "Suspended marketer"),
        )
    except MarketerAuditError:
        _raise_audit_failed("suspend_marketer")
    except MarketerError as exc:
        _raise_for(exc)
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
        marketer = MarketerService(db).reactivate(
            marketer_id,
            audit=_audit_hook(db, current_admin, "reactivate_marketer", "Reactivated marketer"),
        )
    except MarketerAuditError:
        _raise_audit_failed("reactivate_marketer")
    except MarketerError as exc:
        _raise_for(exc)
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
    "amount_mismatch": (
        "The amount matches neither the item's current amount nor its claimed amount "
        "(a reward may have been held); check what you actually paid"
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
    if isinstance(exc, payouts.AmountMismatchError):
        detail["marketer_id"] = str(exc.marketer_id)
        detail["current_amount_kobo"] = exc.current_amount_kobo
        detail["claimed_amount_kobo"] = exc.claimed_amount_kobo
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


@router.post(
    "/batches/prepare",
    response_model=PrepareBatchResponse,
    dependencies=[Depends(platform_admin_rate_limit)],
)
def prepare_batch(
    db: Session = Depends(get_db),
    current_admin: PlatformAdminResponse = Depends(get_current_super_admin),
):
    """Group every payable reward into a new ``draft`` batch right now (the 1st/15th cron does the
    same; this is the re-run when that cron failed). Nothing is sent: a super admin still has to
    approve the batch. Idempotent: with nothing payable it answers ``{"batch": null}``.

    Declared before ``/batches/{batch_id:uuid}``; the uuid convertor means the literal can never
    be read as a batch id either way. The audit entry carries the batch id, totals and counts.
    """
    try:
        batch = payouts.prepare_batch(db, datetime.now(UTC))
    except Exception as exc:
        db.rollback()
        logger.error(
            f"Manual payout batch preparation failed (admin {current_admin.id}): "
            f"{type(exc).__name__}"
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"code": "prepare_failed", "message": "The batch was not prepared"},
        ) from exc
    if batch is None:
        return PrepareBatchResponse(batch=None)
    detail = _detail(db, batch.id)
    _audit_batch(
        db,
        current_admin,
        "prepare_payout_batch",
        detail.id,
        f"Prepared payout batch {detail.id}",
        _counts_metadata(detail),
    )
    return PrepareBatchResponse(batch=detail)


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
    Each entry carries the ``amount_kobo`` actually paid. It must equal the item's current amount
    (``current_amount_kobo``) or, for an awaiting-manual item, its claimed ``amount_kobo`` (the
    admin may have paid it before a reward was held); anything else is refused with 409
    ``amount_mismatch`` (carrying both figures) and nothing changes. The audit entry carries the
    batch id and the number of items, never the bank references."""
    entries = [
        (entry.marketer_id, entry.bank_reference, entry.amount_kobo) for entry in request.items
    ]
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


# --- reward review queue ------------------------------------------------------------------------
# Reads: any platform admin. Release / void decide who gets paid, so they need a SUPER_ADMIN. Both
# write their own audit entry in the same transaction as the status change (see rewards.py).

_REWARD_ERROR_MESSAGES = {
    "reward_not_found": "Reward not found",
    "reward_state_invalid": "The reward is not in a state that allows this action",
    "reward_in_flight": (
        "The reward is still tied to a payout batch; resolve that payout item first"
    ),
}

QueueStatus = Literal["pending", "held", "payable", "batched", "paid", "void", "all"]


def _raise_for_reward(exc: rewards.RewardError) -> NoReturn:
    """Stable ``{code, message}`` bodies. Never echoes ``str(exc)`` (it carries ids)."""
    if isinstance(exc, rewards.RewardNotFoundError):
        http_status = status.HTTP_404_NOT_FOUND
    else:
        http_status = status.HTTP_409_CONFLICT
    code = exc.code if exc.code in _REWARD_ERROR_MESSAGES else "reward_state_invalid"
    raise HTTPException(
        status_code=http_status,
        detail={"code": code, "message": _REWARD_ERROR_MESSAGES[code]},
    ) from exc


@router.get(
    "/rewards",
    response_model=RewardQueueResponse,
    dependencies=[Depends(platform_admin_rate_limit)],
)
def list_rewards(
    status_filter: QueueStatus = Query("held", alias="status"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_admin: PlatformAdminResponse = Depends(get_current_platform_admin),
):
    """The reward review queue, newest conversion first. ``status=held`` (default) is the queue
    proper; ``all`` lists every status. Rewards flagged ``shared_domain`` carry the other tenants
    that share the domain."""
    reward_status = None if status_filter == "all" else RewardStatus(status_filter)
    return review_queue.list_rewards(db, reward_status, limit, offset)


def _decide(
    action: str,
    reward_id: UUID,
    request: RewardActionRequest,
    db: Session,
    admin: PlatformAdminResponse,
) -> RewardActionResponse:
    decide = rewards.release if action == "release" else rewards.void
    try:
        reward = decide(db, reward_id, admin.id, request.note, admin_name=admin.email)
    except rewards.RewardError as exc:
        _raise_for_reward(exc)
    return RewardActionResponse(id=reward.id, status=reward.status)


@router.post(
    "/rewards/{reward_id:uuid}/release",
    response_model=RewardActionResponse,
    dependencies=[Depends(platform_admin_rate_limit)],
)
def release_reward(
    reward_id: UUID,
    request: RewardActionRequest,
    db: Session = Depends(get_db),
    current_admin: PlatformAdminResponse = Depends(get_current_super_admin),
):
    """Clear a held reward: payable if its hold date has passed, else pending. A note is
    required and is kept in the audit entry only."""
    return _decide("release", reward_id, request, db, current_admin)


@router.post(
    "/rewards/{reward_id:uuid}/void",
    response_model=RewardActionResponse,
    dependencies=[Depends(platform_admin_rate_limit)],
)
def void_reward(
    reward_id: UUID,
    request: RewardActionRequest,
    db: Session = Depends(get_db),
    current_admin: PlatformAdminResponse = Depends(get_current_super_admin),
):
    """Void a held, pending or payable reward. A note is required (audit entry only)."""
    return _decide("void", reward_id, request, db, current_admin)


# --- referral config ----------------------------------------------------------------------------


def _config_response(db: Session) -> ReferralConfigResponse:
    config = get_referral_config(db)
    return ReferralConfigResponse(
        reward_kobo=config.reward_kobo,
        hold_days=config.hold_days,
        commission_window_days=config.commission_window_days,
        source_by_key=get_config_sources(db),
    )


@router.get(
    "/config",
    response_model=ReferralConfigResponse,
    dependencies=[Depends(platform_admin_rate_limit)],
)
def get_config(
    db: Session = Depends(get_db),
    current_admin: PlatformAdminResponse = Depends(get_current_platform_admin),
):
    """The effective reward amount and windows, and whether each is an admin override or env."""
    return _config_response(db)


@router.patch(
    "/config",
    response_model=ReferralConfigResponse,
    dependencies=[Depends(platform_admin_rate_limit)],
)
def update_config(
    request: ReferralConfigUpdate,
    db: Session = Depends(get_db),
    current_admin: PlatformAdminResponse = Depends(get_current_super_admin),
):
    """Partially update the programme settings (bounds enforced; integers only).

    The new values only affect rewards created AFTER this call: existing rewards keep the amount
    and hold date they were created with. The change and its audit entry commit together; if the
    audit cannot be written the change is rolled back.
    """
    values = {key: getattr(request, key) for key in CONFIG_KEYS if key in request.model_fields_set}
    try:
        update_referral_config(db, current_admin.id, values)
        # log_action commits, so the flushed change and its record become durable together.
        EnhancedAuditService(db).log_action(
            action_type="update_referral_config",
            performed_by_type="platform_admin",
            performed_by_id=current_admin.id,
            performed_by_name=current_admin.email,
            target_type="referral_config",
            target_id=None,
            description="Updated referral program settings",
            severity="high",
            metadata={"changed": values},
        )
    except Exception as exc:
        db.rollback()
        logger.error(
            f"Failed to update referral config (admin {current_admin.id}): {type(exc).__name__}"
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"code": "config_update_failed", "message": "The settings were not changed"},
        ) from exc
    return _config_response(db)
