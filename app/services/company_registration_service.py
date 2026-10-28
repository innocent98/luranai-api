"""Company registration service for self-service signup."""

from datetime import UTC, datetime, timedelta

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.services.entitlements import new_tenant_capabilities
from app.core.encryption import hash_for_lookup
from app.core.security import get_password_hash
from app.db.models.tenant import Tenant, TenantAIConfig, TenantStatus
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
from app.utils.slugs import PUBLIC_SLUG_MAX_LENGTH, normalize_public_slug


class CompanyRegistrationService:
    """Service for company self-service registration."""

    def __init__(self, db: Session):
        """Initialize company registration service with database session."""
        self.db = db

    def _generate_unique_public_slug(self, company_name: str) -> str:
        """Generate a unique public support slug for a new tenant."""
        base_slug = normalize_public_slug(company_name) or "support"
        candidate = base_slug
        suffix = 2

        while True:
            existing = self.db.execute(
                select(Tenant).where(Tenant.public_slug == candidate)
            ).scalar_one_or_none()
            if not existing:
                return candidate

            suffix_text = f"-{suffix}"
            trimmed = base_slug[: PUBLIC_SLUG_MAX_LENGTH - len(suffix_text)].rstrip("-")
            candidate = f"{trimmed}{suffix_text}"
            suffix += 1

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
        tenant_stmt = select(Tenant).where(Tenant.name == tenant_name)
        existing_tenant = self.db.execute(tenant_stmt).scalar_one_or_none()
        if existing_tenant:
            raise ValueError(f"Company with name '{registration_data.company_name}' already exists")

        # Check if admin email already exists
        user_stmt = select(User).where(
            User.email_hash == hash_for_lookup(registration_data.admin_email)
        )
        existing_user = self.db.execute(user_stmt).scalar_one_or_none()
        if existing_user:
            raise ValueError(f"User with email {registration_data.admin_email} already exists")

        try:
            # Generate temporary password
            temp_password = generate_temp_password()

            # Calculate trial end date (30 days from now)
            trial_ends_at = datetime.now(UTC) + timedelta(days=30)

            # Generate API key for tenant
            import secrets

            api_key = secrets.token_urlsafe(32)
            api_key_hash = get_password_hash(api_key)

            # Create tenant
            display_name = registration_data.company_display_name or registration_data.company_name
            tenant = Tenant(
                name=tenant_name,
                display_name=display_name,
                public_slug=self._generate_unique_public_slug(tenant_name),
                status=TenantStatus.ACTIVE,  # Active immediately, but user needs to verify email
                api_key_hash=api_key_hash,
                is_publicly_listed=True,  # Visible in customer directory by default
                subscription_tier="trial",
                subscription_status="trialing",
                trial_ends_at=trial_ends_at,
            )
            self.db.add(tenant)
            self.db.flush()  # Get tenant ID without committing

            # Create default AI config
            ai_config = TenantAIConfig(
                tenant_id=tenant.id,
                brand_voice={},
                escalation_keywords=[],
                max_response_tokens=450,  # Trial tier default
                grounding_requirement=True,
                grounding_mode="balanced",
                low_confidence_action="clarify",
                no_retrieval_action="clarify",
                first_responder="human",
                require_name_before_escalation=False,
                require_email_before_escalation=False,
                collect_name_before_chat=False,
                collect_email_before_chat=False,
            )
            self.db.add(ai_config)

            # Create trial tier capabilities
            capabilities = new_tenant_capabilities(self.db, tenant.id, "trial")
            self.db.add(capabilities)

            # Create admin user (inactive until email verified)
            admin_user = User(
                email=registration_data.admin_email,
                hashed_password=get_password_hash(temp_password),
                full_name=registration_data.admin_full_name,
                role=UserRole.TENANT_ADMIN,
                tenant_id=tenant.id,
                is_active=False,  # Inactive until email verified
            )
            self.db.add(admin_user)
            self.db.flush()  # Get user ID without committing

            # Create email verification token
            verification_token = token_service.create_email_verification_token(
                user_id=admin_user.id,
                email=admin_user.email,
                expiry_hours=24,
            )

            # Send verification email (credentials will be sent after verification)
            email_sent = email_service.send_verification_email(
                email=admin_user.email,
                user_name=admin_user.full_name,
                verification_token=verification_token,
            )

            if not email_sent:
                logger.warning(
                    f"Failed to send verification email to {admin_user.email}, "
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

        # Generate new temporary password
        temp_password = generate_temp_password()

        # Update user password and activate account
        user.hashed_password = get_password_hash(temp_password)
        user.is_active = True
        user.must_change_password = True

        # Get tenant information for welcome email
        tenant_stmt = select(Tenant).where(Tenant.id == user.tenant_id)
        tenant = self.db.execute(tenant_stmt).scalar_one_or_none()

        if not tenant:
            logger.error(f"Tenant not found for user {user.id}")
            raise ValueError("Tenant not found")

        # Commit database changes before sending email
        self.db.commit()

        logger.info(f"Email verified for user {user.email} (user_id={user.id})")

        # Send welcome email with login credentials
        email_sent = email_service.send_welcome_email(
            company_name=tenant.display_name,
            admin_email=user.email,
            admin_name=user.full_name,
            temp_password=temp_password,
            verification_token=None,  # No verification needed, already verified
        )

        if not email_sent:
            logger.warning(
                f"Failed to send welcome email to {user.email} after verification, "
                "but account is active"
            )

        return EmailVerificationResponse(
            message="Email verified successfully! Check your email for login credentials.",
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
        user_stmt = select(User).where(User.email_hash == hash_for_lookup(email))
        user = self.db.execute(user_stmt).scalar_one_or_none()

        if not user:
            raise ValueError("User not found")

        if user.is_active:
            raise ValueError("Email already verified")

        # Get tenant
        tenant_stmt = select(Tenant).where(Tenant.id == user.tenant_id)
        tenant = self.db.execute(tenant_stmt).scalar_one_or_none()

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
