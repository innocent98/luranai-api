"""Account-deletion request lifecycle endpoints: request / verify / cancel / tenant.

Thin adapters over `AccountDeletionService` (the lifecycle owner — see that
module's docstring). This layer owns three things the service explicitly
does not: HTTP status/error shaping, emailing (reusing `email_service`), and
the public path's anti-enumeration response contract.

Security note on the public path (`POST /account/deletion-request` without a
bearer token): the response body is byte-identical regardless of whether the
email matches an account (`AccountDeletionPublicResponse`'s default is never
overridden). The dominant timing signal — the outbound SMTP call — is moved
off the request/response path entirely via `BackgroundTasks`, so it can
never leak into the HTTP response's latency. The residual signal (one extra
`INSERT` when the account exists, per `AccountDeletionService.request_public`)
is accepted: it is a few hundred microseconds against ordinary network
jitter and is the same posture `AuthService.request_password_reset` already
ships with elsewhere in this codebase. The endpoint is additionally
rate-limited (`account_deletion_public_rate_limiter`, 5/hour/IP) to make any
statistical timing analysis impractical.
"""

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from app.api.deps import (
    extract_token,
    get_current_tenant_admin,
    get_current_user,
    get_db,
    revoke_token,
)
from app.core.config import settings
from app.db.models.user import User as UserModel
from app.schemas.account_deletion import (
    AccountDeletionCancelBody,
    AccountDeletionCancelResponse,
    AccountDeletionGraceResponse,
    AccountDeletionPublicResponse,
    AccountDeletionRequestBody,
    AccountDeletionVerifyBody,
    TenantDeletionRequestBody,
)
from app.schemas.user import UserResponse
from app.services.account_deletion_service import (
    AccountDeletionService,
    AlreadyPurgedError,
    InvalidOrExpiredTokenError,
    LastAdminError,
    NotTenantAdminError,
)
from app.services.email_service import email_service
from app.utils.http_rate_limiter import account_deletion_public_rate_limiter, check_rate_limit

router = APIRouter()


def _extract_jti(token: str | None) -> str | None:
    """Best-effort jti extraction from a bearer/cookie access token.

    Never raises — an invalid/expired token here just means "no jti to
    blacklist", not an auth failure; auth itself is enforced separately
    (via `get_current_user`/`get_current_tenant_admin`) wherever it's
    actually required.
    """
    if not token:
        return None
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
    except JWTError:
        return None
    jti = payload.get("jti")
    return str(jti) if jti else None


def _last_admin_response(exc: LastAdminError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"code": "last_admin", "options": exc.options, "message": str(exc)},
    )


@router.post(
    "/account/deletion-request",
    status_code=status.HTTP_200_OK,
)
async def request_account_deletion(
    request: Request,
    background_tasks: BackgroundTasks,
    payload: AccountDeletionRequestBody | None = None,
    db: Session = Depends(get_db),
):
    """Dual-mode deletion request.

    With a bearer/cookie access token: authed self-serve request for the
    caller's own account. Without one: public, email-based request — always
    returns the same fixed body (see module docstring) and is rate-limited.
    """
    token = extract_token(request)

    if token:
        current_user: UserResponse = await get_current_user(request, db)
        user = db.get(UserModel, current_user.id)
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Could not validate credentials",
            )

        service = AccountDeletionService(db)
        try:
            deletion_request = service.request_self(user, current_jti=_extract_jti(token))
        except LastAdminError as exc:
            raise _last_admin_response(exc) from exc

        # `_enter_grace` always sets both before returning; `raw_cancel_token`
        # is a transient (non-mapped) attribute the service attaches for the
        # caller to email — see `AccountDeletionService`'s module docstring.
        assert deletion_request.scheduled_purge_at is not None
        email_service.send_account_deletion_confirmation_email(
            email=user.email,
            cancel_token=deletion_request.raw_cancel_token,  # type: ignore[attr-defined]
            scheduled_purge_at=deletion_request.scheduled_purge_at,
            frontend_url=settings.FRONTEND_URL,
        )

        return AccountDeletionGraceResponse(
            message="Your account is scheduled for deletion. Check your email for a link to cancel.",
            scheduled_purge_at=deletion_request.scheduled_purge_at,
        )

    # --- Public, unauthenticated path -------------------------------------
    await check_rate_limit(request, account_deletion_public_rate_limiter, "ip")

    email = payload.email if payload else None
    if not email:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="email is required",
        )

    service = AccountDeletionService(db)
    raw_token = service.request_public(email)

    if raw_token:
        background_tasks.add_task(
            email_service.send_account_deletion_verification_email,
            email=email,
            verification_token=raw_token,
            frontend_url=settings.FRONTEND_URL,
        )

    # Constant-shape response regardless of `raw_token` — never branch the
    # body/status on whether the account existed.
    return AccountDeletionPublicResponse()


@router.post(
    "/account/deletion-request/verify",
    response_model=AccountDeletionGraceResponse,
    status_code=status.HTTP_200_OK,
)
def verify_account_deletion(
    payload: AccountDeletionVerifyBody,
    request: Request,
    db: Session = Depends(get_db),
):
    """Verify a public deletion request, moving it into the grace period."""
    service = AccountDeletionService(db)
    try:
        deletion_request = service.verify(
            payload.token, current_jti=_extract_jti(extract_token(request))
        )
    except LastAdminError as exc:
        raise _last_admin_response(exc) from exc
    except InvalidOrExpiredTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "invalid_or_expired_token",
                "message": "Invalid or expired verification token.",
            },
        ) from exc

    assert deletion_request.scheduled_purge_at is not None

    user = db.get(UserModel, deletion_request.user_id)
    if user is not None:
        email_service.send_account_deletion_confirmation_email(
            email=user.email,
            cancel_token=deletion_request.raw_cancel_token,  # type: ignore[attr-defined]
            scheduled_purge_at=deletion_request.scheduled_purge_at,
            frontend_url=settings.FRONTEND_URL,
        )

    return AccountDeletionGraceResponse(
        message="Your account is now scheduled for deletion. Check your email for a link to cancel.",
        scheduled_purge_at=deletion_request.scheduled_purge_at,
    )


@router.post(
    "/account/deletion-request/cancel",
    response_model=AccountDeletionCancelResponse,
    status_code=status.HTTP_200_OK,
)
async def cancel_account_deletion(
    request: Request,
    payload: AccountDeletionCancelBody | None = None,
    db: Session = Depends(get_db),
):
    """Cancel a deletion still in grace — via the emailed token, or authed."""
    raw_token = payload.token if payload else None
    service = AccountDeletionService(db)

    try:
        if raw_token:
            service.cancel(raw_token)
        else:
            current_user: UserResponse = await get_current_user(request, db)
            user = db.get(UserModel, current_user.id)
            if user is None:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Could not validate credentials",
                )
            service.cancel(user)
    except AlreadyPurgedError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "already_purged", "message": str(exc)},
        ) from exc
    except InvalidOrExpiredTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "invalid_or_expired_token",
                "message": "Invalid or expired cancellation token.",
            },
        ) from exc

    return AccountDeletionCancelResponse()


@router.post(
    "/account/deletion-request/tenant",
    response_model=AccountDeletionGraceResponse,
    status_code=status.HTTP_200_OK,
)
def request_tenant_deletion(
    request: Request,
    payload: TenantDeletionRequestBody,
    current_user: UserResponse = Depends(get_current_tenant_admin),
    db: Session = Depends(get_db),
):
    """Whole-tenant deletion — tenant_admin only, requires explicit confirm."""
    if not payload.confirm:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "confirmation_required",
                "message": "Set confirm=true to proceed.",
            },
        )

    admin = db.get(UserModel, current_user.id)
    if admin is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
        )

    service = AccountDeletionService(db)
    try:
        deletion_request = service.request_tenant(admin)
    except NotTenantAdminError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "not_tenant_admin", "message": str(exc)},
        ) from exc

    # `AccountDeletionService.request_tenant` doesn't blacklist the caller's
    # own live access token (it has no per-request jti to pass) — close that
    # gap here, the same way `auth.py`'s `logout()` does for its own jti.
    jti = _extract_jti(extract_token(request))
    if jti:
        revoke_token(jti, settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60)

    assert deletion_request.scheduled_purge_at is not None
    email_service.send_account_deletion_confirmation_email(
        email=admin.email,
        cancel_token=deletion_request.raw_cancel_token,  # type: ignore[attr-defined]
        scheduled_purge_at=deletion_request.scheduled_purge_at,
        frontend_url=settings.FRONTEND_URL,
    )

    return AccountDeletionGraceResponse(
        message="This tenant is scheduled for deletion. Check your email for a link to cancel.",
        scheduled_purge_at=deletion_request.scheduled_purge_at,
    )
