"""Customer engagement service for follows and business channel posts."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import desc, func, or_, select
from sqlalchemy.orm import Session

from app.db.models.customer_engagement import (
    ChannelPostStatus,
    CustomerBusinessFollow,
    TenantChannelPost,
)
from app.db.models.tenant import Tenant
from app.db.models.user import UserRole
from app.schemas.customer_engagement import TenantChannelPostCreate, TenantChannelPostUpdate
from app.schemas.user import UserResponse


class CustomerEngagementService:
    """Encapsulate customer follows and channel publishing flows."""

    def __init__(self, db: Session):
        self.db = db

    def get_tenant(self, tenant_id: UUID) -> Tenant | None:
        return self.db.execute(select(Tenant).where(Tenant.id == tenant_id)).scalar_one_or_none()

    def get_follow(self, customer_user_id: UUID, tenant_id: UUID) -> CustomerBusinessFollow | None:
        return self.db.execute(
            select(CustomerBusinessFollow).where(
                CustomerBusinessFollow.customer_user_id == customer_user_id,
                CustomerBusinessFollow.tenant_id == tenant_id,
            )
        ).scalar_one_or_none()

    def follow_business(self, customer_user_id: UUID, tenant_id: UUID) -> CustomerBusinessFollow:
        follow = self.get_follow(customer_user_id, tenant_id)
        if follow:
            return follow

        tenant = self.get_tenant(tenant_id)
        if not tenant:
            raise ValueError("Business not found")

        follow = CustomerBusinessFollow(
            customer_user_id=customer_user_id,
            tenant_id=tenant_id,
            notifications_enabled=True,
        )
        self.db.add(follow)
        self.db.commit()
        self.db.refresh(follow)
        return follow

    def unfollow_business(self, customer_user_id: UUID, tenant_id: UUID) -> None:
        follow = self.get_follow(customer_user_id, tenant_id)
        if not follow:
            return
        self.db.delete(follow)
        self.db.commit()

    def follower_count(self, tenant_id: UUID) -> int:
        return int(
            self.db.execute(
                select(func.count(CustomerBusinessFollow.id)).where(
                    CustomerBusinessFollow.tenant_id == tenant_id
                )
            ).scalar()
            or 0
        )

    def published_post_count(self, tenant_id: UUID) -> int:
        return int(
            self.db.execute(
                select(func.count(TenantChannelPost.id)).where(
                    TenantChannelPost.tenant_id == tenant_id,
                    TenantChannelPost.status == ChannelPostStatus.PUBLISHED,
                )
            ).scalar()
            or 0
        )

    def list_follows(self, customer_user_id: UUID) -> list[tuple[CustomerBusinessFollow, Tenant, TenantChannelPost | None]]:
        follow_rows = self.db.execute(
            select(CustomerBusinessFollow, Tenant)
            .join(Tenant, Tenant.id == CustomerBusinessFollow.tenant_id)
            .where(CustomerBusinessFollow.customer_user_id == customer_user_id)
            .order_by(desc(CustomerBusinessFollow.created_at))
        ).all()

        tenant_ids = [tenant.id for _, tenant in follow_rows]
        latest_posts: dict[UUID, TenantChannelPost] = {}
        if tenant_ids:
            posts = self.db.execute(
                select(TenantChannelPost)
                .where(
                    TenantChannelPost.tenant_id.in_(tenant_ids),
                    TenantChannelPost.status == ChannelPostStatus.PUBLISHED,
                )
                .order_by(
                    TenantChannelPost.tenant_id,
                    desc(TenantChannelPost.is_pinned),
                    desc(TenantChannelPost.published_at),
                )
            ).scalars()
            for post in posts:
                latest_posts.setdefault(post.tenant_id, post)

        return [(follow, tenant, latest_posts.get(tenant.id)) for follow, tenant in follow_rows]

    def list_customer_feed(
        self,
        customer_user_id: UUID,
        following_only: bool = True,
        tenant_id: UUID | None = None,
        limit: int = 30,
        before: datetime | None = None,
    ) -> tuple[list[tuple[TenantChannelPost, Tenant, bool]], bool]:
        follow_subquery = (
            select(CustomerBusinessFollow.tenant_id)
            .where(CustomerBusinessFollow.customer_user_id == customer_user_id)
            .subquery()
        )

        stmt = (
            select(
                TenantChannelPost,
                Tenant,
                CustomerBusinessFollow.id.is_not(None).label("is_following"),
            )
            .join(Tenant, Tenant.id == TenantChannelPost.tenant_id)
            .outerjoin(
                CustomerBusinessFollow,
                (CustomerBusinessFollow.tenant_id == TenantChannelPost.tenant_id)
                & (CustomerBusinessFollow.customer_user_id == customer_user_id),
            )
            .where(TenantChannelPost.status == ChannelPostStatus.PUBLISHED)
        )

        if tenant_id:
            stmt = stmt.where(TenantChannelPost.tenant_id == tenant_id)

        if following_only:
            stmt = stmt.where(TenantChannelPost.tenant_id.in_(select(follow_subquery.c.tenant_id)))
        else:
            stmt = stmt.where(or_(Tenant.is_publicly_listed.is_(True), CustomerBusinessFollow.id.is_not(None)))

        if before:
            stmt = stmt.where(TenantChannelPost.published_at < before)

        rows = self.db.execute(
            stmt.order_by(
                desc(TenantChannelPost.is_pinned),
                desc(TenantChannelPost.published_at),
            ).limit(limit + 1)
        ).all()

        has_more = len(rows) > limit
        if has_more:
            rows = rows[:limit]

        return [(row[0], row[1], bool(row[2])) for row in rows], has_more

    def get_customer_post(
        self,
        customer_user_id: UUID,
        post_id: UUID,
    ) -> tuple[TenantChannelPost, Tenant, bool] | None:
        row = self.db.execute(
            select(
                TenantChannelPost,
                Tenant,
                CustomerBusinessFollow.id.is_not(None).label("is_following"),
            )
            .join(Tenant, Tenant.id == TenantChannelPost.tenant_id)
            .outerjoin(
                CustomerBusinessFollow,
                (CustomerBusinessFollow.tenant_id == TenantChannelPost.tenant_id)
                & (CustomerBusinessFollow.customer_user_id == customer_user_id),
            )
            .where(
                TenantChannelPost.id == post_id,
                TenantChannelPost.status == ChannelPostStatus.PUBLISHED,
            )
        ).first()
        if not row:
            return None
        return row[0], row[1], bool(row[2])

    def _assert_manage_permission(self, current_user: UserResponse) -> None:
        if current_user.role not in {UserRole.TENANT_ADMIN, UserRole.SUPPORT_AGENT}:
            raise ValueError("You do not have permission to manage channels")

    def list_tenant_posts(
        self,
        current_user: UserResponse,
    ) -> list[TenantChannelPost]:
        self._assert_manage_permission(current_user)
        return list(
            self.db.execute(
                select(TenantChannelPost)
                .where(TenantChannelPost.tenant_id == current_user.tenant_id)
                .order_by(
                    desc(TenantChannelPost.is_pinned),
                    desc(TenantChannelPost.published_at),
                    desc(TenantChannelPost.created_at),
                )
            ).scalars()
        )

    def create_tenant_post(
        self,
        current_user: UserResponse,
        payload: TenantChannelPostCreate,
    ) -> TenantChannelPost:
        self._assert_manage_permission(current_user)

        post = TenantChannelPost(
            tenant_id=current_user.tenant_id,
            created_by_user_id=current_user.id,
            title=payload.title.strip(),
            excerpt=payload.excerpt.strip(),
            content=payload.content.strip(),
            cover_image_url=payload.cover_image_url,
            cta_label=payload.cta_label,
            cta_url=payload.cta_url,
            is_pinned=payload.is_pinned,
        )
        if payload.publish_now:
            post.status = ChannelPostStatus.PUBLISHED
            post.published_at = datetime.now(UTC)
            post.published_by_user_id = current_user.id

        self.db.add(post)
        self.db.commit()
        self.db.refresh(post)
        return post

    def get_tenant_post(self, current_user: UserResponse, post_id: UUID) -> TenantChannelPost | None:
        self._assert_manage_permission(current_user)
        return self.db.execute(
            select(TenantChannelPost).where(
                TenantChannelPost.id == post_id,
                TenantChannelPost.tenant_id == current_user.tenant_id,
            )
        ).scalar_one_or_none()

    def update_tenant_post(
        self,
        current_user: UserResponse,
        post_id: UUID,
        payload: TenantChannelPostUpdate,
    ) -> TenantChannelPost:
        post = self.get_tenant_post(current_user, post_id)
        if not post:
            raise ValueError("Channel post not found")

        for field in (
            "title",
            "excerpt",
            "content",
            "cover_image_url",
            "cta_label",
            "cta_url",
            "is_pinned",
        ):
            value = getattr(payload, field)
            if value is not None:
                setattr(post, field, value.strip() if isinstance(value, str) else value)

        self.db.commit()
        self.db.refresh(post)
        return post

    def publish_tenant_post(self, current_user: UserResponse, post_id: UUID) -> TenantChannelPost:
        post = self.get_tenant_post(current_user, post_id)
        if not post:
            raise ValueError("Channel post not found")

        post.status = ChannelPostStatus.PUBLISHED
        post.published_at = datetime.now(UTC)
        post.published_by_user_id = current_user.id
        self.db.commit()
        self.db.refresh(post)
        return post

    def archive_tenant_post(self, current_user: UserResponse, post_id: UUID) -> TenantChannelPost:
        post = self.get_tenant_post(current_user, post_id)
        if not post:
            raise ValueError("Channel post not found")

        post.status = ChannelPostStatus.ARCHIVED
        self.db.commit()
        self.db.refresh(post)
        return post

    def tenant_overview(self, current_user: UserResponse) -> dict[str, int]:
        self._assert_manage_permission(current_user)
        counts = self.db.execute(
            select(TenantChannelPost.status, func.count(TenantChannelPost.id))
            .where(TenantChannelPost.tenant_id == current_user.tenant_id)
            .group_by(TenantChannelPost.status)
        ).all()
        status_counts = {status.value: count for status, count in counts}
        return {
            "drafts": int(status_counts.get(ChannelPostStatus.DRAFT.value, 0)),
            "published": int(status_counts.get(ChannelPostStatus.PUBLISHED.value, 0)),
            "archived": int(status_counts.get(ChannelPostStatus.ARCHIVED.value, 0)),
            "followers": self.follower_count(current_user.tenant_id),
        }
