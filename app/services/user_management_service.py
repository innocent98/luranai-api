"""User management service for platform admins."""

from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from loguru import logger
from sqlalchemy import and_, desc, func, select
from sqlalchemy.orm import Session

from app.core.encryption import hash_for_lookup
from app.db.models.audit import AuditLog
from app.db.models.subscription_plan import SubscriptionPlan
from app.db.models.tenant import Tenant, TenantCapabilities
from app.db.models.user import User, UserRole


class UserManagementService:
    """Service for cross-tenant user management."""

    def __init__(self, db: Session):
        """
        Initialize user management service.

        Args:
            db: Database session
        """
        self.db = db

    def search_users(
        self,
        email: str | None = None,
        tenant_id: UUID | None = None,
        role: UserRole | None = None,
        is_active: bool | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """
        Search users across tenants with filters.

        Args:
            email: Email filter (partial match)
            tenant_id: Filter by tenant
            role: Filter by user role
            is_active: Filter by active status
            limit: Max results (default 100, max 1000)
            offset: Results offset

        Returns:
            Dict with users and pagination info
        """
        try:
            # Build query with filters
            query = select(User)

            # Apply filters
            filters: list[Any] = []

            if email:
                filters.append(User.email.ilike(f"%{email}%"))

            if tenant_id:
                filters.append(User.tenant_id == tenant_id)

            if role:
                filters.append(User.role == role)

            if is_active is not None:
                filters.append(User.is_active == is_active)

            if filters:
                query = query.where(and_(*filters))

            # Get total count
            count_query = select(func.count()).select_from(User)
            if filters:
                count_query = count_query.where(and_(*filters))

            total_result = self.db.execute(count_query)
            total_count = total_result.scalar() or 0

            # Apply ordering, limit, and offset
            query = query.order_by(desc(User.created_at))
            query = query.limit(min(limit, 1000)).offset(offset)

            # Execute query
            result = self.db.execute(query)
            users = result.scalars().all()

            # Get tenant names for each user
            user_list = []
            for user in users:
                tenant_query = select(Tenant).where(Tenant.id == user.tenant_id)
                tenant_result = self.db.execute(tenant_query)
                tenant = tenant_result.scalars().first()

                user_list.append(
                    {
                        "id": str(user.id),
                        "email": user.email,
                        "full_name": user.full_name,
                        "role": user.role.value,
                        "is_active": user.is_active,
                        "tenant_id": str(user.tenant_id),
                        "tenant_name": tenant.name if tenant else "Unknown",
                        "tenant_display_name": tenant.display_name if tenant else "Unknown",
                        "created_at": user.created_at.isoformat(),
                        "updated_at": user.updated_at.isoformat(),
                        "last_login_at": (
                            user.last_login_at.isoformat() if user.last_login_at else None
                        ),
                    }
                )

            return {
                "users": user_list,
                "pagination": {
                    "total": total_count,
                    "limit": limit,
                    "offset": offset,
                    "has_more": (offset + len(user_list)) < total_count,
                },
            }

        except Exception as e:
            logger.error(f"Error searching users: {e}")
            raise

    def get_user_details(
        self,
        user_id: UUID,
    ) -> dict[str, Any]:
        """
        Get detailed information about a specific user.

        Args:
            user_id: User ID

        Returns:
            Dict with user details

        Raises:
            ValueError: If user not found
        """
        try:
            # Get user
            user_query = select(User).where(User.id == user_id)
            user_result = self.db.execute(user_query)
            user = user_result.scalars().first()

            if not user:
                raise ValueError("User not found")

            # Get tenant info
            tenant_query = select(Tenant).where(Tenant.id == user.tenant_id)
            tenant_result = self.db.execute(tenant_query)
            tenant = tenant_result.scalars().first()

            # Get user's recent activity (last 30 days)
            activity = self.get_user_activity(user_id, days=30)

            return {
                "id": str(user.id),
                "email": user.email,
                "full_name": user.full_name,
                "role": user.role.value,
                "is_active": user.is_active,
                "tenant": {
                    "id": str(tenant.id) if tenant else None,
                    "name": tenant.name if tenant else "Unknown",
                    "display_name": tenant.display_name if tenant else "Unknown",
                    "subscription_status": tenant.subscription_status if tenant else None,
                    "subscription_tier": tenant.subscription_tier if tenant else None,
                },
                "created_at": user.created_at.isoformat(),
                "updated_at": user.updated_at.isoformat(),
                "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
                "recent_activity": activity,
            }

        except ValueError:
            raise
        except Exception as e:
            logger.error(f"Error getting user details: {e}")
            raise

    def get_user_activity(
        self,
        user_id: UUID,
        days: int = 30,
    ) -> dict[str, Any]:
        """
        Get user's activity summary from audit logs.

        Args:
            user_id: User ID
            days: Number of days to analyze

        Returns:
            Dict with activity summary
        """
        try:
            end_date = datetime.utcnow()
            start_date = end_date - timedelta(days=days)

            # Get total actions
            total_query = select(func.count(AuditLog.id)).where(
                and_(
                    AuditLog.user_id == user_id,
                    AuditLog.created_at >= start_date,
                )
            )
            total_result = self.db.execute(total_query)
            total_actions = total_result.scalar() or 0

            # Get actions by type
            actions_query = (
                select(AuditLog.action, func.count(AuditLog.id).label("count"))
                .where(
                    and_(
                        AuditLog.user_id == user_id,
                        AuditLog.created_at >= start_date,
                    )
                )
                .group_by(AuditLog.action)
            )

            actions_result = self.db.execute(actions_query)
            actions_breakdown: dict[str, int] = {
                str(action): int(count) for action, count in actions_result.all()
            }

            # Get most recent actions
            recent_query = (
                select(AuditLog)
                .where(AuditLog.user_id == user_id)
                .order_by(desc(AuditLog.created_at))
                .limit(10)
            )

            recent_result = self.db.execute(recent_query)
            recent_actions = recent_result.scalars().all()

            recent_list = [
                {
                    "action": action.action,
                    "resource_type": action.resource_type,
                    "created_at": action.created_at.isoformat(),
                }
                for action in recent_actions
            ]

            return {
                "period_days": days,
                "total_actions": total_actions,
                "actions_breakdown": actions_breakdown,
                "recent_actions": recent_list,
            }

        except Exception as e:
            logger.error(f"Error getting user activity: {e}")
            raise

    def deactivate_user_platform_admin(
        self,
        user_id: UUID,
        reason: str,
        admin_id: UUID,
    ) -> dict[str, Any]:
        """
        Deactivate a user account (platform admin only).

        Args:
            user_id: User ID to deactivate
            reason: Reason for deactivation
            admin_id: Platform admin who performed the action

        Returns:
            Dict with deactivation status

        Raises:
            ValueError: If user not found or already inactive
        """
        try:
            # Get user
            user_query = select(User).where(User.id == user_id)
            user_result = self.db.execute(user_query)
            user = user_result.scalars().first()

            if not user:
                raise ValueError("User not found")

            if not user.is_active:
                raise ValueError("User is already inactive")

            # Deactivate user
            user.is_active = False
            user.updated_at = datetime.utcnow()

            # Create audit log
            audit_log = AuditLog(
                tenant_id=user.tenant_id,
                user_id=admin_id,
                action="user_deactivated",
                resource_type="user",
                resource_id=user.id,
                changes={
                    "user_id": str(user.id),
                    "user_email": user.email,
                    "reason": reason,
                    "deactivated_by": str(admin_id),
                },
            )

            self.db.add(audit_log)
            self.db.commit()
            self.db.refresh(user)

            return {
                "status": "success",
                "message": f"User {user.email} has been deactivated",
                "user_id": str(user.id),
                "reason": reason,
                "deactivated_at": datetime.utcnow().isoformat(),
            }

        except ValueError:
            raise
        except Exception as e:
            self.db.rollback()
            logger.error(f"Error deactivating user: {e}")
            raise

    def get_role_distribution(self) -> dict[str, Any]:
        """
        Get distribution of users by role.

        Returns:
            Dict with role distribution statistics
        """
        try:
            # Get total users
            total_query = select(func.count(User.id))
            total_result = self.db.execute(total_query)
            total_users = total_result.scalar() or 0

            # Get active users
            active_query = select(func.count(User.id)).where(User.is_active.is_(True))
            active_result = self.db.execute(active_query)
            active_users = active_result.scalar() or 0

            # Get users by role
            role_query = select(User.role, func.count(User.id).label("count")).group_by(User.role)

            role_result = self.db.execute(role_query)
            role_distribution = {role.value: count for role, count in role_result.all()}

            # Get users created in last 30 days
            thirty_days_ago = datetime.utcnow() - timedelta(days=30)
            recent_query = select(func.count(User.id)).where(User.created_at >= thirty_days_ago)
            recent_result = self.db.execute(recent_query)
            recent_users = recent_result.scalar() or 0

            return {
                "total_users": total_users,
                "active_users": active_users,
                "inactive_users": total_users - active_users,
                "role_distribution": role_distribution,
                "recent_signups_30d": recent_users,
            }

        except Exception as e:
            logger.error(f"Error getting role distribution: {e}")
            raise

    def invite_user(
        self,
        invite_data,
        tenant_id: UUID,
        invited_by_name: str,
    ):
        """
        Invite a new team member (tenant-scoped).

        Args:
            invite_data: User invitation data
            tenant_id: Tenant ID
            invited_by_name: Name of user who invited them

        Returns:
            Created user response

        Raises:
            ValueError: If user already exists or invalid role
        """
        from app.core.security import get_password_hash
        from app.db.models.user import UserRole
        from app.schemas.user import UserResponse
        from app.services.email_service import email_service
        from app.utils.password import generate_temp_password

        try:
            # Enforce team member limit from current tenant plan (or tenant override)
            max_team_members = self._get_max_team_members_limit(tenant_id)
            if max_team_members is not None:
                active_count = (
                    self.db.execute(
                        select(func.count())
                        .select_from(User)
                        .where(User.tenant_id == tenant_id, User.is_active.is_(True))
                    ).scalar()
                    or 0
                )
                if active_count >= max_team_members:
                    raise ValueError(
                        f"Team member limit reached. Your plan allows {max_team_members} active team members. "
                        "Please upgrade your subscription to invite more users."
                    )

            # Check if user already exists
            existing_query = select(User).where(
                User.email_hash == hash_for_lookup(invite_data.email)
            )
            existing_result = self.db.execute(existing_query)
            existing_user = existing_result.scalars().first()

            if existing_user:
                raise ValueError(f"User with email {invite_data.email} already exists")

            # Validate role (only SUPPORT_AGENT or VIEWER allowed for team invites)
            if invite_data.role not in [UserRole.SUPPORT_AGENT, UserRole.VIEWER]:
                raise ValueError("Invalid role. Only SUPPORT_AGENT or VIEWER allowed")

            # Generate temporary password
            temp_password = generate_temp_password()

            # Create user
            new_user = User(
                email=invite_data.email,
                hashed_password=get_password_hash(temp_password),
                full_name=invite_data.full_name,
                role=invite_data.role,
                tenant_id=tenant_id,
                is_active=True,  # Active immediately after invite
            )

            self.db.add(new_user)
            self.db.flush()

            # Send invite email
            email_service.send_user_invite_email(
                user_email=new_user.email,
                user_name=new_user.full_name,
                company_name=self._get_tenant_name(tenant_id),
                role=invite_data.role.value,
                temp_password=temp_password,
                invited_by=invited_by_name,
            )

            self.db.commit()
            self.db.refresh(new_user)

            logger.info(f"User {new_user.email} invited to tenant {tenant_id}")

            return UserResponse(
                id=new_user.id,
                email=new_user.email,
                full_name=new_user.full_name,
                role=new_user.role,
                tenant_id=new_user.tenant_id,
                is_active=new_user.is_active,
                created_at=new_user.created_at,
                updated_at=new_user.updated_at,
            )

        except ValueError:
            raise
        except Exception as e:
            self.db.rollback()
            logger.error(f"Error inviting user: {e}")
            raise

    def _get_tenant_name(self, tenant_id: UUID) -> str:
        """Get tenant display name."""
        tenant_query = select(Tenant).where(Tenant.id == tenant_id)
        tenant_result = self.db.execute(tenant_query)
        tenant = tenant_result.scalars().first()
        return tenant.display_name if tenant else "Unknown"

    def _get_max_team_members_limit(self, tenant_id: UUID) -> int | None:
        """Resolve effective max team members for a tenant."""
        tenant = self.db.execute(select(Tenant).where(Tenant.id == tenant_id)).scalars().first()
        if not tenant:
            raise ValueError("Tenant not found")

        capabilities = (
            self.db.execute(
                select(TenantCapabilities).where(TenantCapabilities.tenant_id == tenant_id)
            )
            .scalars()
            .first()
        )
        if capabilities and capabilities.max_team_members is not None:
            return capabilities.max_team_members

        plan = (
            self.db.execute(
                select(SubscriptionPlan).where(
                    SubscriptionPlan.tier == tenant.subscription_tier,
                    SubscriptionPlan.is_active.is_(True),
                )
            )
            .scalars()
            .first()
        )
        if not plan:
            plan = (
                self.db.execute(
                    select(SubscriptionPlan).where(
                        SubscriptionPlan.tier == tenant.subscription_tier
                    )
                )
                .scalars()
                .first()
            )
        if not plan:
            raise ValueError(f"Subscription plan not found for tier: {tenant.subscription_tier}")

        return plan.max_team_members

    def get_user_by_id(self, user_id: UUID, tenant_id: UUID):
        """
        Get user by ID within tenant (tenant-scoped).

        Args:
            user_id: User ID
            tenant_id: Tenant ID

        Returns:
            User response or None

        Raises:
            ValueError: If user not in tenant
        """
        from app.schemas.user import UserResponse

        try:
            user_query = select(User).where(User.id == user_id, User.tenant_id == tenant_id)
            user_result = self.db.execute(user_query)
            user = user_result.scalars().first()

            if not user:
                return None

            return UserResponse(
                id=user.id,
                email=user.email,
                full_name=user.full_name,
                role=user.role,
                tenant_id=user.tenant_id,
                is_active=user.is_active,
                created_at=user.created_at,
                updated_at=user.updated_at,
            )

        except Exception as e:
            logger.error(f"Error getting user by ID: {e}")
            raise

    def update_user(self, user_id: UUID, tenant_id: UUID, update_data):
        """
        Update user within tenant (tenant-scoped).

        Args:
            user_id: User ID
            tenant_id: Tenant ID
            update_data: User update data

        Returns:
            Updated user response

        Raises:
            ValueError: If user not found or trying to update admin
        """
        from app.db.models.user import UserRole
        from app.schemas.user import UserResponse

        try:
            user_query = select(User).where(User.id == user_id, User.tenant_id == tenant_id)
            user_result = self.db.execute(user_query)
            user = user_result.scalars().first()

            if not user:
                raise ValueError("User not found")

            # Prevent updating tenant admin
            if user.role == UserRole.TENANT_ADMIN:
                raise ValueError("Cannot update tenant admin user")

            # Update fields
            if update_data.full_name is not None:
                user.full_name = update_data.full_name
            if update_data.role is not None:
                if update_data.role not in [UserRole.SUPPORT_AGENT, UserRole.VIEWER]:
                    raise ValueError("Invalid role")
                user.role = update_data.role

            user.updated_at = datetime.utcnow()

            self.db.commit()
            self.db.refresh(user)

            logger.info(f"User {user.email} updated")

            return UserResponse(
                id=user.id,
                email=user.email,
                full_name=user.full_name,
                role=user.role,
                tenant_id=user.tenant_id,
                is_active=user.is_active,
                created_at=user.created_at,
                updated_at=user.updated_at,
            )

        except ValueError:
            raise
        except Exception as e:
            self.db.rollback()
            logger.error(f"Error updating user: {e}")
            raise

    def deactivate_user(self, user_id: UUID, tenant_id: UUID):
        """
        Deactivate user within tenant (tenant-scoped).

        Args:
            user_id: User ID
            tenant_id: Tenant ID

        Raises:
            ValueError: If user not found or trying to deactivate admin
        """
        from app.db.models.user import UserRole

        try:
            user_query = select(User).where(User.id == user_id, User.tenant_id == tenant_id)
            user_result = self.db.execute(user_query)
            user = user_result.scalars().first()

            if not user:
                raise ValueError("User not found")

            if user.role == UserRole.TENANT_ADMIN:
                raise ValueError("Cannot deactivate tenant admin")

            if not user.is_active:
                raise ValueError("User is already inactive")

            user.is_active = False
            user.updated_at = datetime.utcnow()

            self.db.commit()

            logger.info(f"User {user.email} deactivated")

        except ValueError:
            raise
        except Exception as e:
            self.db.rollback()
            logger.error(f"Error deactivating user: {e}")
            raise

    def regenerate_password(self, user_id: UUID, tenant_id: UUID):
        """
        Regenerate password for user within tenant.

        Args:
            user_id: User ID
            tenant_id: Tenant ID

        Returns:
            Response with new temporary password

        Raises:
            ValueError: If user not found
        """
        from app.core.security import get_password_hash
        from app.schemas.user import UserRegeneratePasswordResponse
        from app.utils.password import generate_temp_password

        try:
            user_query = select(User).where(User.id == user_id, User.tenant_id == tenant_id)
            user_result = self.db.execute(user_query)
            user = user_result.scalars().first()

            if not user:
                raise ValueError("User not found")

            # Generate new temporary password
            temp_password = generate_temp_password()

            # Update user password
            user.hashed_password = get_password_hash(temp_password)
            user.must_change_password = True
            user.updated_at = datetime.utcnow()

            self.db.commit()

            logger.info(f"Password regenerated for user {user.email}")

            # TODO: Send email with new password
            # For now, email_sent is False since we're not sending emails yet

            return UserRegeneratePasswordResponse(
                message="Password regenerated successfully",
                temp_password=temp_password,
                email_sent=False,
            )

        except ValueError:
            raise
        except Exception as e:
            self.db.rollback()
            logger.error(f"Error regenerating password: {e}")
            raise
