"""Company registration endpoints for self-service signup."""

from loguru import logger
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.auditor.service import AuditService
from app.schemas.auth import (
    CompanyRegistrationRequest,
    CompanyRegistrationResponse,
    EmailVerificationRequest,
    EmailVerificationResponse,
)
from app.services.company_registration_service import CompanyRegistrationService
from app.utils.http_rate_limiter import (
    registration_rate_limit,
    resend_verification_rate_limit,
    verification_rate_limit,
)

router = APIRouter()


@router.post(
    "/register-company",
    response_model=CompanyRegistrationResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(registration_rate_limit)],
)
async def register_company(
    registration_data: CompanyRegistrationRequest,
    db: Session = Depends(get_db),
):
    """
    Self-service company registration endpoint.

    Creates a new tenant and admin user atomically, generates temporary password,
    and sends welcome email with email verification link.

    If a ``claim_token`` is present in the request the corresponding public
    audit is re-parented to the new tenant after the tenant is created.  Any
    failure during claim (invalid/expired/already-claimed token, Redis error)
    is logged as a warning and silently swallowed — signup is never blocked by
    a bad claim token.

    Args:
        registration_data: Company and admin information
        db: Database session

    Returns:
        Registration response with tenant ID and verification status

    Raises:
        HTTPException: 400 if company name or email already exists
    """
    registration_service = CompanyRegistrationService(db)

    try:
        response = registration_service.register_company(registration_data)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        ) from e

    if registration_data.claim_token:
        try:
            await AuditService(db).claim_audit(
                registration_data.claim_token, response.tenant_id
            )
            logger.info(
                f"Claimed public audit (token=…{registration_data.claim_token[-6:]}) "
                f"for tenant {response.tenant_id}"
            )
        except Exception:
            logger.warning(
                "Failed to claim public audit on signup "
                f"(token=…{registration_data.claim_token[-6:]}, "
                f"tenant={response.tenant_id}) — signup continues",
                exc_info=True,
            )

    return response


@router.post(
    "/verify-email",
    response_model=EmailVerificationResponse,
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(verification_rate_limit)],
)
def verify_email(
    verification_request: EmailVerificationRequest,
    db: Session = Depends(get_db),
):
    """
    Verify user email address using token.

    Activates the user account once email is verified.

    Args:
        verification_request: Email verification token
        db: Database session

    Returns:
        Verification response

    Raises:
        HTTPException: 400 if token is invalid or expired
    """
    registration_service = CompanyRegistrationService(db)

    try:
        response = registration_service.verify_email(verification_request)
        return response
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        ) from e


@router.post(
    "/resend-verification",
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(resend_verification_rate_limit)],
)
def resend_verification_email(
    email: str,
    db: Session = Depends(get_db),
):
    """
    Resend verification email to user.

    Args:
        email: User email address
        db: Database session

    Returns:
        Success message

    Raises:
        HTTPException: 400 if user not found or already verified
    """
    registration_service = CompanyRegistrationService(db)

    try:
        email_sent = registration_service.resend_verification_email(email)
        if email_sent:
            return {"message": "Verification email sent successfully"}
        else:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to send verification email",
            )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        ) from e
