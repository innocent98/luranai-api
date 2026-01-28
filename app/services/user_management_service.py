"""User management service for platform admins."""

from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
from uuid import UUID

from loguru import logger
from sqlalchemy import select, func, and_, or_, desc
from sqlalchemy.orm import Session

from app.db.models.user import User, UserRole
from app.db.models.tenant import Tenant
from app.db.models.audit import AuditLog


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
        email: Optional[str] = None,
        tenant_id: Optional[UUID] = None,
        role: Optional[UserRole] = None,
        is_active: Optional[bool] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Dict[str, Any]:
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
            filters = []

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

                user_list.append({
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
                    "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
                })

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
    ) -> Dict[str, Any]:
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
    ) -> Dict[str, Any]:
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
            actions_query = select(
                AuditLog.action,
                func.count(AuditLog.id).label("count")
            ).where(
                and_(
                    AuditLog.user_id == user_id,
                    AuditLog.created_at >= start_date,
                )
            ).group_by(AuditLog.action)

            actions_result = self.db.execute(actions_query)
            actions_breakdown = {action: count for action, count in actions_result.all()}

            # Get most recent actions
            recent_query = select(AuditLog).where(
                AuditLog.user_id == user_id
            ).order_by(desc(AuditLog.created_at)).limit(10)

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

    def deactivate_user(
        self,
        user_id: UUID,
        reason: str,
        admin_id: UUID,
    ) -> Dict[str, Any]:
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

    def get_role_distribution(self) -> Dict[str, Any]:
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
            active_query = select(func.count(User.id)).where(User.is_active == True)
            active_result = self.db.execute(active_query)
            active_users = active_result.scalar() or 0

            # Get users by role
            role_query = select(
                User.role,
                func.count(User.id).label("count")
            ).group_by(User.role)

            role_result = self.db.execute(role_query)
            role_distribution = {role.value: count for role, count in role_result.all()}

            # Get users created in last 30 days
            thirty_days_ago = datetime.utcnow() - timedelta(days=30)
            recent_query = select(func.count(User.id)).where(
                User.created_at >= thirty_days_ago
            )
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
