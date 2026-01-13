"""Authentication service for user management and JWT tokens."""

from datetime import datetime, timedelta
from typing import Optional
from uuid import UUID

from sqlalchemy.orm import Session
from sqlalchemy import select

from app.core.config import settings
from app.core.security import verify_password, get_password_hash, create_access_token
from app.db.models.user import User, UserRole
from app.schemas.user import (
    UserCreate,
    UserResponse,
    Token,
    TokenData,
    LoginRequest,
)


class AuthService:
    """Service for authentication and user management."""

    def __init__(self, db: Session):
        """Initialize auth service with database session."""
        self.db = db

    def authenticate_user(self, email: str, password: str) -> Optional[User]:
        """
        Authenticate a user by email and password.

        Args:
            email: User email address
            password: Plain text password

        Returns:
            User object if authentication successful, None otherwise
        """
        stmt = select(User).where(User.email == email, User.is_active == True)
        user = self.db.execute(stmt).scalar_one_or_none()

        if not user:
            return None

        if not verify_password(password, user.hashed_password):
            return None

        # Update last login timestamp
        user.last_login_at = datetime.utcnow()
        self.db.commit()

        return user

    def login(self, login_data: LoginRequest) -> Token:
        """
        Login user and generate JWT token.

        Args:
            login_data: Login credentials

        Returns:
            Token with access token and expiration

        Raises:
            ValueError: If authentication fails
        """
        user = self.authenticate_user(login_data.email, login_data.password)

        if not user:
            raise ValueError("Incorrect email or password")

        # Generate access token with user ID as subject and additional claims
        access_token = create_access_token(
            subject=str(user.id),
            additional_claims={
                "email": user.email,
                "role": user.role.value,
                "tenant_id": str(user.tenant_id) if user.tenant_id else None,
            }
        )

        return Token(
            access_token=access_token,
            token_type="bearer",
            expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        )

    def create_user(self, user_data: UserCreate) -> UserResponse:
        """
        Create a new user.

        Args:
            user_data: User creation data

        Returns:
            Created user response

        Raises:
            ValueError: If user with email already exists
        """
        # Check if user exists
        stmt = select(User).where(User.email == user_data.email)
        existing_user = self.db.execute(stmt).scalar_one_or_none()

        if existing_user:
            raise ValueError(f"User with email {user_data.email} already exists")

        # Create new user
        db_user = User(
            email=user_data.email,
            hashed_password=get_password_hash(user_data.password),
            full_name=user_data.full_name,
            role=user_data.role,
            tenant_id=user_data.tenant_id,
            is_active=True,
            is_superuser=(user_data.role == UserRole.SUPER_ADMIN),
        )

        self.db.add(db_user)
        self.db.commit()
        self.db.refresh(db_user)

        return UserResponse.model_validate(db_user)

    def get_user_by_id(self, user_id: UUID) -> Optional[UserResponse]:
        """
        Get user by ID.

        Args:
            user_id: User UUID

        Returns:
            User response if found, None otherwise
        """
        stmt = select(User).where(User.id == user_id)
        user = self.db.execute(stmt).scalar_one_or_none()

        if not user:
            return None

        return UserResponse.model_validate(user)

    def get_user_by_email(self, email: str) -> Optional[UserResponse]:
        """
        Get user by email.

        Args:
            email: User email address

        Returns:
            User response if found, None otherwise
        """
        stmt = select(User).where(User.email == email)
        user = self.db.execute(stmt).scalar_one_or_none()

        if not user:
            return None

        return UserResponse.model_validate(user)

    def verify_token_data(self, token_data: dict) -> Optional[TokenData]:
        """
        Verify and extract data from JWT token payload.

        Args:
            token_data: Decoded JWT payload

        Returns:
            TokenData if valid, None otherwise
        """
        try:
            user_id = UUID(token_data.get("sub"))
            email = token_data.get("email")
            role = UserRole(token_data.get("role"))
            tenant_id = UUID(token_data.get("tenant_id")) if token_data.get("tenant_id") else None

            return TokenData(
                user_id=user_id,
                email=email,
                role=role,
                tenant_id=tenant_id,
            )
        except (ValueError, KeyError, TypeError):
            return None

    def update_user_password(self, user_id: UUID, current_password: str, new_password: str) -> bool:
        """
        Update user password.

        Args:
            user_id: User UUID
            current_password: Current password for verification
            new_password: New password to set

        Returns:
            True if password updated successfully

        Raises:
            ValueError: If user not found or current password incorrect
        """
        stmt = select(User).where(User.id == user_id)
        user = self.db.execute(stmt).scalar_one_or_none()

        if not user:
            raise ValueError("User not found")

        if not verify_password(current_password, user.hashed_password):
            raise ValueError("Current password is incorrect")

        # Update password
        user.hashed_password = get_password_hash(new_password)
        self.db.commit()

        return True

    def deactivate_user(self, user_id: UUID) -> bool:
        """
        Deactivate a user account.

        Args:
            user_id: User UUID

        Returns:
            True if deactivated successfully

        Raises:
            ValueError: If user not found
        """
        stmt = select(User).where(User.id == user_id)
        user = self.db.execute(stmt).scalar_one_or_none()

        if not user:
            raise ValueError("User not found")

        user.is_active = False
        self.db.commit()

        return True

    def get_users_by_tenant(self, tenant_id: UUID) -> list[UserResponse]:
        """
        Get all users for a specific tenant.

        Args:
            tenant_id: Tenant UUID

        Returns:
            List of user responses
        """
        stmt = select(User).where(User.tenant_id == tenant_id, User.is_active == True)
        users = self.db.execute(stmt).scalars().all()

        return [UserResponse.model_validate(user) for user in users]
