"""User management service for TENANT_ADMIN to manage team members."""

from typing import Optional
from uuid import UUID

from sqlalchemy.orm import Session
from sqlalchemy import select
from loguru import logger

from app.core.security import get_password_hash
from app.db.models.user import User, UserRole
from app.db.models.tenant import Tenant
from app.schemas.user import (
    UserInviteRequest,
    UserResponse,
    UserListResponse,
    UserUpdate,
    UserRegeneratePasswordResponse,
)
from app.services.email_service import email_service
from app.utils.password import generate_temp_password


class UserManagementService:
    """Service for managing team members within a tenant."""

    def __init__(self, db: Session):
        """Initialize user management service with database session."""
        self.db = db

    def invite_user(
        self,
        invite_data: UserInviteRequest,
        tenant_id: UUID,
        invited_by_name: str,
    ) -> UserResponse:
        """
        Invite a new team member (TENANT_ADMIN only).

        Creates a new user with SUPPORT_AGENT or VIEWER role,
        generates temporary password, and sends invite email.

        Args:
            invite_data: User invitation data
            tenant_id: Tenant ID of the inviting admin
            invited_by_name: Name of the person inviting

        Returns:
            Created user response

        Raises:
            ValueError: If user already exists or invalid role
        """
        # Validate role (only SUPPORT_AGENT and VIEWER allowed)
        if invite_data.role not in [UserRole.SUPPORT_AGENT, UserRole.VIEWER]:
            raise ValueError(
                "Invalid role. Only SUPPORT_AGENT and VIEWER roles can be created by tenant admins."
            )

        # Check if user with email already exists
        stmt = select(User).where(User.email == invite_data.email)
        existing_user = self.db.execute(stmt).scalar_one_or_none()
        if existing_user:
            raise ValueError(f"User with email {invite_data.email} already exists")

        # Get tenant info for email
        stmt = select(Tenant).where(Tenant.id == tenant_id)
        tenant = self.db.execute(stmt).scalar_one_or_none()
        if not tenant:
            raise ValueError("Tenant not found")

        try:
            # Generate temporary password
            temp_password = generate_temp_password()

            # Create user
            user = User(
                email=invite_data.email,
                hashed_password=get_password_hash(temp_password),
                full_name=invite_data.full_name,
                role=invite_data.role,
                tenant_id=tenant_id,
                is_active=True,  # Active immediately for invited users
            )

            self.db.add(user)
            self.db.flush()  # Get user ID

            # Send invite email
            email_sent = email_service.send_user_invite_email(
                user_email=user.email,
                user_name=user.full_name,
                company_name=tenant.display_name,
                role=user.role.value,
                temp_password=temp_password,
                invited_by=invited_by_name,
            )

            if not email_sent:
                logger.warning(
                    f"Failed to send invite email to {user.email}, "
                    "but user creation will proceed"
                )

            self.db.commit()

            logger.info(
                f"User {user.email} invited to tenant {tenant_id} "
                f"with role {user.role.value}"
            )

            return UserResponse.model_validate(user)

        except Exception as e:
            self.db.rollback()
            logger.error(f"Failed to invite user: {e}")
            raise

    def get_user_by_id(
        self, user_id: UUID, tenant_id: UUID
    ) -> Optional[UserResponse]:
        """
        Get user by ID within a tenant.

        Args:
            user_id: User UUID
            tenant_id: Tenant UUID (for access control)

        Returns:
            User response if found, None otherwise
        """
        stmt = select(User).where(
            User.id == user_id, User.tenant_id == tenant_id
        )
        user = self.db.execute(stmt).scalar_one_or_none()

        if not user:
            return None

        return UserResponse.model_validate(user)

    def list_users(self, tenant_id: UUID) -> list[UserListResponse]:
        """
        List all users in a tenant.

        Args:
            tenant_id: Tenant UUID

        Returns:
            List of user responses
        """
        stmt = select(User).where(User.tenant_id == tenant_id)
        users = self.db.execute(stmt).scalars().all()

        return [UserListResponse.model_validate(user) for user in users]

    def update_user(
        self, user_id: UUID, tenant_id: UUID, update_data: UserUpdate
    ) -> UserResponse:
        """
        Update user information.

        Args:
            user_id: User UUID
            tenant_id: Tenant UUID (for access control)
            update_data: User update data

        Returns:
            Updated user response

        Raises:
            ValueError: If user not found or invalid role
        """
        # Get user
        stmt = select(User).where(
            User.id == user_id, User.tenant_id == tenant_id
        )
        user = self.db.execute(stmt).scalar_one_or_none()

        if not user:
            raise ValueError("User not found")

        # Validate role change (can't change to TENANT_ADMIN)
        if update_data.role and update_data.role == UserRole.TENANT_ADMIN:
            raise ValueError("Cannot change user role to TENANT_ADMIN")

        # Update fields
        if update_data.full_name is not None:
            user.full_name = update_data.full_name

        if update_data.role is not None:
            user.role = update_data.role

        if update_data.is_active is not None:
            user.is_active = update_data.is_active

        self.db.commit()
        self.db.refresh(user)

        logger.info(f"Updated user {user.email} (user_id={user.id})")

        return UserResponse.model_validate(user)

    def deactivate_user(self, user_id: UUID, tenant_id: UUID) -> bool:
        """
        Deactivate a user (soft delete).

        Args:
            user_id: User UUID
            tenant_id: Tenant UUID (for access control)

        Returns:
            True if deactivated successfully

        Raises:
            ValueError: If user not found or is TENANT_ADMIN
        """
        # Get user
        stmt = select(User).where(
            User.id == user_id, User.tenant_id == tenant_id
        )
        user = self.db.execute(stmt).scalar_one_or_none()

        if not user:
            raise ValueError("User not found")

        # Prevent deactivating TENANT_ADMIN
        if user.role == UserRole.TENANT_ADMIN:
            raise ValueError("Cannot deactivate TENANT_ADMIN user")

        user.is_active = False
        self.db.commit()

        logger.info(f"Deactivated user {user.email} (user_id={user.id})")

        return True

    def regenerate_password(
        self, user_id: UUID, tenant_id: UUID
    ) -> UserRegeneratePasswordResponse:
        """
        Regenerate user password and send via email.

        Args:
            user_id: User UUID
            tenant_id: Tenant UUID (for access control)

        Returns:
            Response with new temporary password

        Raises:
            ValueError: If user not found
        """
        # Get user
        stmt = select(User).where(
            User.id == user_id, User.tenant_id == tenant_id
        )
        user = self.db.execute(stmt).scalar_one_or_none()

        if not user:
            raise ValueError("User not found")

        # Get tenant info
        stmt = select(Tenant).where(Tenant.id == tenant_id)
        tenant = self.db.execute(stmt).scalar_one_or_none()

        # Generate new temporary password
        new_password = generate_temp_password()
        user.hashed_password = get_password_hash(new_password)
        self.db.commit()

        # Send email with new password
        email_sent = False
        if tenant:
            # Use invite email for password reset (contains credentials)
            email_sent = email_service.send_user_invite_email(
                user_email=user.email,
                user_name=user.full_name,
                company_name=tenant.display_name,
                role=user.role.value,
                temp_password=new_password,
                invited_by="System (Password Reset)",
            )

        logger.info(f"Regenerated password for user {user.email} (user_id={user.id})")

        return UserRegeneratePasswordResponse(
            message="Password regenerated successfully",
            temp_password=new_password,
            email_sent=email_sent,
        )
