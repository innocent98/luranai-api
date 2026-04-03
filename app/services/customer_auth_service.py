"""Customer authentication and profile management."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import create_access_token, get_password_hash, verify_password
from app.db.models.customer_user import CustomerUser
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
    CustomerTokenData,
    CustomerUserResponse,
)
from app.schemas.user import Token
from app.services.email_service import email_service
from app.services.token_service import token_service


class CustomerAuthService:
    """Service for B2C customer auth lifecycle."""

    def __init__(self, db: Session):
        self.db = db

    @property
    def frontend_url(self) -> str:
        return settings.CUSTOMER_FRONTEND_URL or settings.FRONTEND_URL

    def _build_auth_response(self, customer: CustomerUser) -> CustomerAuthResponse:
        access_token = create_access_token(
            subject=str(customer.id),
            additional_claims={
                "email": customer.email,
                "auth_type": "customer",
            },
        )
        return CustomerAuthResponse(
            token=Token(
                access_token=access_token,
                token_type="bearer",
                expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
            ),
            customer=CustomerUserResponse.model_validate(customer),
        )

    def get_customer_by_id(self, customer_user_id: UUID) -> CustomerUserResponse | None:
        customer = self.db.execute(
            select(CustomerUser).where(CustomerUser.id == customer_user_id)
        ).scalar_one_or_none()
        if not customer:
            return None
        return CustomerUserResponse.model_validate(customer)

    def verify_token_data(self, token_data: dict) -> CustomerTokenData | None:
        try:
            if token_data.get("auth_type") != "customer":
                return None
            return CustomerTokenData(
                customer_user_id=UUID(token_data.get("sub")),
                email=str(token_data.get("email")),
            )
        except (TypeError, ValueError):
            return None

    def register_customer(self, payload: CustomerRegisterRequest) -> CustomerActionResponse:
        if payload.password != payload.confirm_password:
            raise ValueError("Passwords do not match")
        if not payload.accepted_terms:
            raise ValueError("Terms must be accepted")

        existing_customer = self.db.execute(
            select(CustomerUser).where(CustomerUser.email == payload.email.lower().strip())
        ).scalar_one_or_none()
        if existing_customer:
            raise ValueError("A customer with this email already exists")

        customer = CustomerUser(
            full_name=payload.full_name.strip(),
            email=payload.email.lower().strip(),
            hashed_password=get_password_hash(payload.password),
            is_active=False,
        )
        self.db.add(customer)
        self.db.flush()

        verification_token = token_service.create_customer_email_verification_token(
            customer_user_id=customer.id,
            email=customer.email,
        )
        email_service.send_verification_email(
            email=customer.email,
            user_name=customer.full_name,
            verification_token=verification_token,
            frontend_url=self.frontend_url,
        )

        self.db.commit()
        return CustomerActionResponse(
            message="Account created. Please check your email to verify your account."
        )

    def verify_email(
        self, payload: CustomerEmailVerificationRequest
    ) -> CustomerActionResponse:
        token_data = token_service.verify_customer_email_token(payload.token)
        if not token_data:
            raise ValueError("Invalid or expired verification token")

        customer = self.db.execute(
            select(CustomerUser).where(CustomerUser.id == token_data["customer_user_id"])
        ).scalar_one_or_none()
        if not customer or customer.email != token_data["email"]:
            raise ValueError("Customer not found")

        if customer.email_verified_at is None:
            customer.email_verified_at = datetime.now(UTC)
        customer.is_active = True
        self.db.commit()

        return CustomerActionResponse(message="Email verified successfully. You can now sign in.")

    def resend_verification(
        self, payload: CustomerResendVerificationRequest
    ) -> CustomerActionResponse:
        customer = self.db.execute(
            select(CustomerUser).where(CustomerUser.email == payload.email.lower().strip())
        ).scalar_one_or_none()
        if not customer:
            raise ValueError("Customer not found")
        if customer.email_verified_at:
            raise ValueError("Email already verified")

        verification_token = token_service.create_customer_email_verification_token(
            customer_user_id=customer.id,
            email=customer.email,
        )
        email_service.send_verification_email(
            email=customer.email,
            user_name=customer.full_name,
            verification_token=verification_token,
            frontend_url=self.frontend_url,
        )
        return CustomerActionResponse(message="Verification email sent successfully.")

    def login(self, payload: CustomerLoginRequest) -> CustomerAuthResponse:
        customer = self.db.execute(
            select(CustomerUser).where(CustomerUser.email == payload.email.lower().strip())
        ).scalar_one_or_none()
        if not customer or not verify_password(payload.password, customer.hashed_password):
            raise ValueError("Incorrect email or password")
        if not customer.is_active or not customer.email_verified_at:
            raise ValueError("Please verify your email before signing in")

        customer.last_login_at = datetime.now(UTC)
        self.db.commit()
        self.db.refresh(customer)
        return self._build_auth_response(customer)

    def request_password_reset(
        self, payload: CustomerForgotPasswordRequest
    ) -> CustomerActionResponse:
        customer = self.db.execute(
            select(CustomerUser).where(CustomerUser.email == payload.email.lower().strip())
        ).scalar_one_or_none()
        if customer:
            reset_token = token_service.create_customer_password_reset_token(
                customer_user_id=customer.id,
                email=customer.email,
            )
            email_service.send_password_reset_email(
                email=customer.email,
                user_name=customer.full_name,
                reset_token=reset_token,
                frontend_url=self.frontend_url,
            )
        return CustomerActionResponse(
            message="If an account exists for that email, a reset link has been sent."
        )

    def reset_password(self, payload: CustomerResetPasswordRequest) -> CustomerActionResponse:
        token_data = token_service.verify_customer_password_reset_token(payload.token)
        if not token_data:
            raise ValueError("Invalid or expired reset token")

        customer = self.db.execute(
            select(CustomerUser).where(CustomerUser.id == token_data["customer_user_id"])
        ).scalar_one_or_none()
        if not customer or customer.email != token_data["email"]:
            raise ValueError("Customer not found")

        customer.hashed_password = get_password_hash(payload.new_password)
        customer.is_active = True
        if customer.email_verified_at is None:
            customer.email_verified_at = datetime.now(UTC)
        self.db.commit()
        return CustomerActionResponse(message="Password reset successfully.")

    def update_profile(
        self,
        customer_user_id: UUID,
        payload: CustomerProfileUpdateRequest,
    ) -> CustomerUserResponse:
        customer = self.db.execute(
            select(CustomerUser).where(CustomerUser.id == customer_user_id)
        ).scalar_one_or_none()
        if not customer:
            raise ValueError("Customer not found")

        customer.full_name = payload.full_name.strip()
        self.db.commit()
        self.db.refresh(customer)
        return CustomerUserResponse.model_validate(customer)

    def update_password(
        self,
        customer_user_id: UUID,
        payload: CustomerPasswordUpdateRequest,
    ) -> CustomerActionResponse:
        customer = self.db.execute(
            select(CustomerUser).where(CustomerUser.id == customer_user_id)
        ).scalar_one_or_none()
        if not customer:
            raise ValueError("Customer not found")
        if not verify_password(payload.current_password, customer.hashed_password):
            raise ValueError("Current password is incorrect")

        customer.hashed_password = get_password_hash(payload.new_password)
        self.db.commit()
        return CustomerActionResponse(message="Password updated successfully.")
