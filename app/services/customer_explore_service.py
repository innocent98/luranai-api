"""Customer-facing explore service — personal block."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models.conversation import ConversationSession
from app.db.models.customer_engagement import CustomerBusinessFollow
from app.db.models.tenant import Tenant
from app.schemas.explore import (
    PersonalBusinessesResponse,
    PersonalBusinessItem,
    PersonalBusinessRelationship,
)

PERSONAL_BLOCK_LIMIT = 8
RECENT_CHAT_WINDOW_DAYS = 30


@dataclass
class _Entry:
    tenant_id: UUID
    is_followed: bool
    followed_at: datetime | None
    last_viewed_channel_at: datetime | None
    last_chat_at: datetime | None

    @property
    def last_activity_at(self) -> datetime:
        """Most recent of followed_at, last_viewed_channel_at, last_chat_at."""
        candidates = [self.followed_at, self.last_viewed_channel_at, self.last_chat_at]
        non_null = [c for c in candidates if c is not None]
        # Construction guarantees at least one non-null timestamp (either a follow
        # or a recent chat must exist to create an entry).
        return max(non_null)


class CustomerExploreService:
    """Read-only service for customer-facing explore surfaces (personal block)."""

    def __init__(self, db: Session):
        self.db = db

    def get_personal_businesses(
        self,
        *,
        customer_user_id: UUID,
    ) -> PersonalBusinessesResponse:
        thirty_days_ago = datetime.now(UTC) - timedelta(days=RECENT_CHAT_WINDOW_DAYS)

        # 1. Followed tenants — all of them, no recency gate on follows
        followed_rows = self.db.execute(
            select(
                CustomerBusinessFollow.tenant_id,
                CustomerBusinessFollow.created_at,
                CustomerBusinessFollow.last_viewed_channel_at,
            ).where(CustomerBusinessFollow.customer_user_id == customer_user_id)
        ).all()

        # 2. Recently-chatted tenants (last 30 days), aggregated per tenant
        recent_chat_rows = self.db.execute(
            select(
                ConversationSession.tenant_id,
                func.max(ConversationSession.last_message_at).label("last_chat_at"),
            )
            .where(
                ConversationSession.customer_user_id == customer_user_id,
                ConversationSession.last_message_at >= thirty_days_ago,
            )
            .group_by(ConversationSession.tenant_id)
        ).all()

        # 3. Merge in Python — keyed by tenant_id, follow info takes precedence
        entries: dict[UUID, _Entry] = {}
        for r in followed_rows:
            entries[r.tenant_id] = _Entry(
                tenant_id=r.tenant_id,
                is_followed=True,
                followed_at=r.created_at,
                last_viewed_channel_at=r.last_viewed_channel_at,
                last_chat_at=None,
            )
        for r in recent_chat_rows:
            if r.tenant_id in entries:
                entries[r.tenant_id].last_chat_at = r.last_chat_at
            else:
                entries[r.tenant_id] = _Entry(
                    tenant_id=r.tenant_id,
                    is_followed=False,
                    followed_at=None,
                    last_viewed_channel_at=None,
                    last_chat_at=r.last_chat_at,
                )

        if not entries:
            return PersonalBusinessesResponse(businesses=[])

        # 4. Sort by last_activity_at DESC, take top N before the DB round-trip
        sorted_entries = sorted(
            entries.values(),
            key=lambda e: e.last_activity_at,
            reverse=True,
        )[:PERSONAL_BLOCK_LIMIT]

        # 5. Fetch tenant rows for the survivors — only publicly listed
        tenant_ids = [e.tenant_id for e in sorted_entries]
        tenants = (
            self.db.execute(
                select(Tenant).where(
                    Tenant.id.in_(tenant_ids),
                    Tenant.is_publicly_listed.is_(True),
                )
            )
            .scalars()
            .all()
        )
        by_id = {t.id: t for t in tenants}

        # 6. Combine, preserving sorted order, dropping unlisted survivors
        items: list[PersonalBusinessItem] = []
        for e in sorted_entries:
            t = by_id.get(e.tenant_id)
            if t is None:
                continue
            items.append(
                PersonalBusinessItem(
                    public_slug=t.public_slug,
                    display_name=t.display_name,
                    description=t.description,
                    support_headline=t.support_headline,
                    support_subheadline=t.support_subheadline,
                    widget_logo_url=t.widget_logo_url,
                    is_featured=t.is_featured,
                    category=t.category,
                    created_at=t.created_at,
                    relationship=PersonalBusinessRelationship(
                        is_followed=e.is_followed,
                        followed_at=e.followed_at,
                        last_chat_at=e.last_chat_at,
                        last_activity_at=e.last_activity_at,
                    ),
                )
            )

        return PersonalBusinessesResponse(businesses=items)
