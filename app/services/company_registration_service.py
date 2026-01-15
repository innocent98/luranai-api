"""Company registration service for self-service signup."""

from uuid import UUID
from sqlalchemy.orm import Session
from sqlalchemy import select
from loguru import logger

from app.core.security import get_password_hash
from app.db.models.tenant import Tenant, TenantStatus
from app.db.models.user import User, UserRole
from app.schemas.auth import (
    CompanyRegistrationRequest,
    CompanyRegistrationResponse,
    EmailVerificationRequest,
    EmailVerificationResponse,
)
from app.services.email_service import email_service
from app.services.token_service import token_service
from app.utils.password import generate_temp_password


class CompanyRegistrationService:
    """Service for company self-service registration."""

    def __init__(self, db: Session):
        """Initialize company registration service with database session."""
        self.db = db

    def register_company(
        self, registration_data: CompanyRegistrationRequest
    ) -> CompanyRegistrationResponse:
        """
        Register a new company with self-service signup.

        This method atomically creates:
        1. A new tenant for the company
        2. A TENANT_ADMIN user for the company admin
        3. An email verification token
        4. Sends welcome email with credentials

        Args:
            registration_data: Company registration data

        Returns:
            Registration response with tenant ID and verification status

        Raises:
            ValueError: If company name or email already exists
        """
        # Normalize company name for tenant name (lowercase, no spaces)
        tenant_name = registration_data.company_name.lower().replace(" ", "_")

        # Check if tenant name already exists
        stmt = select(Tenant).where(Tenant.name == tenant_name)
        existing_tenant = self.db.execute(stmt).scalar_one_or_none()
        if existing_tenant:
            raise ValueError(f"Company with name '{registration_data.company_name}' already exists")

        # Check if admin email already exists
        stmt = select(User).where(User.email == registration_data.admin_email)
        existing_user = self.db.execute(stmt).scalar_one_or_none()
        if existing_user:
            raise ValueError(f"User with email {registration_data.admin_email} already exists")

        try:
            # Generate temporary password
            temp_password = generate_temp_password()

            # Create tenant
            display_name = (
                registration_data.company_display_name
                or registration_data.company_name
            )
            tenant = Tenant(
                name=tenant_name,
                display_name=display_name,
                status=TenantStatus.ACTIVE,  # Active immediately, but user needs to verify email
            )
            self.db.add(tenant)
            self.db.flush()  # Get tenant ID without committing

            # Create admin user (inactive until email verified)
            admin_user = User(
                email=registration_data.admin_email,
                hashed_password=get_password_hash(temp_password),
                full_name=registration_data.admin_full_name,
                role=UserRole.TENANT_ADMIN,
                tenant_id=tenant.id,
                is_active=False,  # Inactive until email verified
                is_superuser=False,
            )
            self.db.add(admin_user)
            self.db.flush()  # Get user ID without committing

            # Create email verification token
            verification_token = token_service.create_email_verification_token(
                user_id=admin_user.id,
                email=admin_user.email,
                expiry_hours=24,
            )

            # Send welcome email with credentials and verification link
            email_sent = email_service.send_welcome_email(
                company_name=display_name,
                admin_email=admin_user.email,
                admin_name=admin_user.full_name,
                temp_password=temp_password,
                verification_token=verification_token,
            )

            if not email_sent:
                logger.warning(
                    f"Failed to send welcome email to {admin_user.email}, "
                    "but registration will proceed"
                )

            # Commit transaction
            self.db.commit()

            logger.info(
                f"Successfully registered company '{display_name}' "
                f"with tenant_id={tenant.id}, admin={admin_user.email}"
            )

            return CompanyRegistrationResponse(
                message="Company registered successfully. Please check your email to verify your account.",
                tenant_id=tenant.id,
                admin_email=admin_user.email,
                verification_required=True,
            )

        except Exception as e:
            self.db.rollback()
            logger.error(f"Failed to register company: {e}")
            raise

    def verify_email(
        self, verification_request: EmailVerificationRequest
    ) -> EmailVerificationResponse:
        """
        Verify user email address using token.

        Args:
            verification_request: Email verification token

        Returns:
            Verification response

        Raises:
            ValueError: If token is invalid or user not found
        """
        # Verify token
        token_data = token_service.verify_email_token(verification_request.token)

        if not token_data:
            raise ValueError("Invalid or expired verification token")

        # Get user
        stmt = select(User).where(User.id == token_data["user_id"])
        user = self.db.execute(stmt).scalar_one_or_none()

        if not user:
            raise ValueError("User not found")

        # Check if email matches
        if user.email != token_data["email"]:
            raise ValueError("Email mismatch")

        # Check if already verified
        if user.is_active:
            return EmailVerificationResponse(
                message="Email already verified",
                verified=True,
            )

        # Activate user
        user.is_active = True
        self.db.commit()

        logger.info(f"Email verified for user {user.email} (user_id={user.id})")

        return EmailVerificationResponse(
            message="Email verified successfully! You can now log in.",
            verified=True,
        )

    def resend_verification_email(self, email: str) -> bool:
        """
        Resend verification email to user.

        Args:
            email: User email address

        Returns:
            True if email sent successfully

        Raises:
            ValueError: If user not found or already verified
        """
        # Get user
        stmt = select(User).where(User.email == email)
        user = self.db.execute(stmt).scalar_one_or_none()

        if not user:
            raise ValueError("User not found")

        if user.is_active:
            raise ValueError("Email already verified")

        # Get tenant
        stmt = select(Tenant).where(Tenant.id == user.tenant_id)
        tenant = self.db.execute(stmt).scalar_one_or_none()

        if not tenant:
            raise ValueError("Tenant not found")

        # Create new verification token
        verification_token = token_service.create_email_verification_token(
            user_id=user.id,
            email=user.email,
            expiry_hours=24,
        )

        # Send verification email
        email_sent = email_service.send_verification_email(
            email=user.email,
            user_name=user.full_name,
            verification_token=verification_token,
        )

        if email_sent:
            logger.info(f"Resent verification email to {user.email}")

        return email_sent
