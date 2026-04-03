"""Customer-facing authentication endpoints."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_customer, get_db
from app.schemas.customer_auth import (
    CustomerActionResponse,
    CustomerAuthResponse,
    CustomerEmailVerificationRequest,
    CustomerForgotPasswordRequest,
    CustomerLoginRequest,
    CustomerPasswordUpdateRequest,
    CustomerProfileUpdateRequest,
    CustomerRegisterRequest,
    CustomerResendVerificationRequest,
    CustomerResetPasswordRequest,
    CustomerUserResponse,
)
from app.services.customer_auth_service import CustomerAuthService
from app.utils.http_rate_limiter import auth_rate_limit

router = APIRouter()


@router.post(
    "/register",
    response_model=CustomerActionResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(auth_rate_limit)],
)
def register_customer(payload: CustomerRegisterRequest, db: Session = Depends(get_db)):
    service = CustomerAuthService(db)
    try:
        return service.register_customer(payload)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post(
    "/verify-email",
    response_model=CustomerActionResponse,
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(auth_rate_limit)],
)
def verify_customer_email(
    payload: CustomerEmailVerificationRequest,
    db: Session = Depends(get_db),
):
    service = CustomerAuthService(db)
    try:
        return service.verify_email(payload)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post(
    "/resend-verification",
    response_model=CustomerActionResponse,
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(auth_rate_limit)],
)
def resend_customer_verification(
    payload: CustomerResendVerificationRequest,
    db: Session = Depends(get_db),
):
    service = CustomerAuthService(db)
    try:
        return service.resend_verification(payload)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post(
    "/login",
    response_model=CustomerAuthResponse,
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(auth_rate_limit)],
)
def login_customer(payload: CustomerLoginRequest, db: Session = Depends(get_db)):
    service = CustomerAuthService(db)
    try:
        return service.login(payload)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc


@router.post(
    "/forgot-password",
    response_model=CustomerActionResponse,
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(auth_rate_limit)],
)
def forgot_customer_password(
    payload: CustomerForgotPasswordRequest,
    db: Session = Depends(get_db),
):
    service = CustomerAuthService(db)
    return service.request_password_reset(payload)


@router.post(
    "/reset-password",
    response_model=CustomerActionResponse,
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(auth_rate_limit)],
)
def reset_customer_password(
    payload: CustomerResetPasswordRequest,
    db: Session = Depends(get_db),
):
    service = CustomerAuthService(db)
    try:
        return service.reset_password(payload)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get("/me", response_model=CustomerUserResponse, status_code=status.HTTP_200_OK)
def get_me(current_customer: CustomerUserResponse = Depends(get_current_customer)):
    return current_customer


@router.put("/me", response_model=CustomerUserResponse, status_code=status.HTTP_200_OK)
def update_profile(
    payload: CustomerProfileUpdateRequest,
    current_customer: CustomerUserResponse = Depends(get_current_customer),
    db: Session = Depends(get_db),
):
    service = CustomerAuthService(db)
    try:
        return service.update_profile(current_customer.id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.put("/me/password", response_model=CustomerActionResponse, status_code=status.HTTP_200_OK)
def update_customer_password(
    payload: CustomerPasswordUpdateRequest,
    current_customer: CustomerUserResponse = Depends(get_current_customer),
    db: Session = Depends(get_db),
):
    service = CustomerAuthService(db)
    try:
        return service.update_password(current_customer.id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
