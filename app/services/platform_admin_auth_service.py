"""Authentication service for platform admin management and JWT tokens."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import create_access_token, get_password_hash, verify_password
from app.db.models.platform_admin import PlatformAdmin, PlatformAdminRole
from app.schemas.platform_admin import (
    PlatformAdminCreate,
    PlatformAdminLoginRequest,
    PlatformAdminResponse,
    PlatformAdminToken,
    PlatformAdminTokenData,
)


class PlatformAdminAuthService:
    """Service for platform admin authentication and management."""

    def __init__(self, db: Session):
        """Initialize platform admin auth service with database session."""
        self.db = db

    def authenticate_admin(self, email: str, password: str) -> PlatformAdmin | None:
        """
        Authenticate a platform admin by email and password.

        Args:
            email: Admin email address
            password: Plain text password

        Returns:
            PlatformAdmin object if authentication successful, None otherwise
        """
        stmt = select(PlatformAdmin).where(
            PlatformAdmin.email == email, PlatformAdmin.is_active.is_(True)
        )
        admin = self.db.execute(stmt).scalar_one_or_none()

        if not admin:
            return None

        if not verify_password(password, admin.hashed_password):
            return None

        # Update last login timestamp
        admin.last_login_at = datetime.utcnow()
        self.db.commit()

        return admin

    def login(self, login_data: PlatformAdminLoginRequest) -> PlatformAdminToken:
        """
        Login platform admin and generate JWT token.

        Args:
            login_data: Login credentials

        Returns:
            Token with access token and expiration

        Raises:
            ValueError: If authentication fails
        """
        admin = self.authenticate_admin(login_data.email, login_data.password)

        if not admin:
            raise ValueError("Incorrect email or password")

        # Generate access token with admin ID as subject and additional claims
        access_token = create_access_token(
            subject=str(admin.id),
            additional_claims={
                "email": admin.email,
                "role": admin.role.value,
                "admin_type": "platform_admin",  # Distinguish from regular users
            },
        )

        return PlatformAdminToken(
            access_token=access_token,
            token_type="bearer",
            expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        )

    def create_admin(self, admin_data: PlatformAdminCreate) -> PlatformAdminResponse:
        """
        Create a new platform admin (SUPER_ADMIN only).

        Args:
            admin_data: Admin creation data

        Returns:
            Created admin response

        Raises:
            ValueError: If admin with email already exists
        """
        # Check if admin exists
        stmt = select(PlatformAdmin).where(PlatformAdmin.email == admin_data.email)
        existing_admin = self.db.execute(stmt).scalar_one_or_none()

        if existing_admin:
            raise ValueError(f"Admin with email {admin_data.email} already exists")

        # Create new platform admin
        db_admin = PlatformAdmin(
            email=admin_data.email,
            hashed_password=get_password_hash(admin_data.password),
            full_name=admin_data.full_name,
            role=admin_data.role,
            is_active=True,
        )

        self.db.add(db_admin)
        self.db.commit()
        self.db.refresh(db_admin)

        return PlatformAdminResponse.model_validate(db_admin)

    def get_admin_by_id(self, admin_id: UUID) -> PlatformAdminResponse | None:
        """
        Get platform admin by ID.

        Args:
            admin_id: Admin UUID

        Returns:
            Admin response if found, None otherwise
        """
        stmt = select(PlatformAdmin).where(PlatformAdmin.id == admin_id)
        admin = self.db.execute(stmt).scalar_one_or_none()

        if not admin:
            return None

        return PlatformAdminResponse.model_validate(admin)

    def get_admin_by_email(self, email: str) -> PlatformAdminResponse | None:
        """
        Get platform admin by email.

        Args:
            email: Admin email address

        Returns:
            Admin response if found, None otherwise
        """
        stmt = select(PlatformAdmin).where(PlatformAdmin.email == email)
        admin = self.db.execute(stmt).scalar_one_or_none()

        if not admin:
            return None

        return PlatformAdminResponse.model_validate(admin)

    def verify_token_data(self, token_data: dict) -> PlatformAdminTokenData | None:
        """
        Verify and extract data from JWT token payload.

        Args:
            token_data: Decoded JWT payload

        Returns:
            PlatformAdminTokenData if valid, None otherwise
        """
        try:
            # Verify this is a platform admin token
            if token_data.get("admin_type") != "platform_admin":
                return None

            admin_id = UUID(token_data.get("sub"))
            email = token_data.get("email")
            if not email:
                return None
            role = PlatformAdminRole(token_data.get("role"))

            return PlatformAdminTokenData(
                admin_id=admin_id,
                email=str(email),
                role=role,
            )
        except (ValueError, KeyError, TypeError):
            return None

    def update_admin_password(
        self, admin_id: UUID, current_password: str, new_password: str
    ) -> bool:
        """
        Update platform admin password.

        Args:
            admin_id: Admin UUID
            current_password: Current password for verification
            new_password: New password to set

        Returns:
            True if password updated successfully

        Raises:
            ValueError: If admin not found or current password incorrect
        """
        stmt = select(PlatformAdmin).where(PlatformAdmin.id == admin_id)
        admin = self.db.execute(stmt).scalar_one_or_none()

        if not admin:
            raise ValueError("Admin not found")

        if not verify_password(current_password, admin.hashed_password):
            raise ValueError("Current password is incorrect")

        # Update password
        admin.hashed_password = get_password_hash(new_password)
        self.db.commit()

        return True

    def deactivate_admin(self, admin_id: UUID) -> bool:
        """
        Deactivate a platform admin account.

        Args:
            admin_id: Admin UUID

        Returns:
            True if deactivated successfully

        Raises:
            ValueError: If admin not found
        """
        stmt = select(PlatformAdmin).where(PlatformAdmin.id == admin_id)
        admin = self.db.execute(stmt).scalar_one_or_none()

        if not admin:
            raise ValueError("Admin not found")

        admin.is_active = False
        self.db.commit()

        return True

    def get_all_admins(self) -> list[PlatformAdminResponse]:
        """
        Get all platform admins (active only).

        Returns:
            List of admin responses
        """
        stmt = select(PlatformAdmin).where(PlatformAdmin.is_active.is_(True))
        admins = self.db.execute(stmt).scalars().all()

        return [PlatformAdminResponse.model_validate(admin) for admin in admins]
