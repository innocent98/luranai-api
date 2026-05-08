"""Service layer for visitor identity and visitor session bootstrap."""

import secrets
from datetime import UTC, datetime, timedelta
from math import ceil
from typing import Any
from uuid import UUID

from sqlalchemy import asc, case, desc, func, or_, select
from sqlalchemy.orm import Session

from app.core.encryption import hash_for_lookup
from app.db.models.conversation import Message, SessionStatus
from app.db.models.tenant import Tenant, TenantStatus
from app.db.models.visitor import DeviceType, Visitor, VisitorSession, VisitorSource
from app.schemas.explore import TenantCategory
from app.schemas.visitor import (
    PublicBusinessDirectoryItem,
    PublicBusinessDirectoryResponse,
    PublicBusinessProfileResponse,
    VisitorBootstrapRequest,
    VisitorBootstrapResponse,
    VisitorConversationMessageSummary,
    VisitorConversationSummary,
    VisitorEnrichmentUpdate,
    VisitorProfileSummary,
)
from app.utils.categories import coerce_tenant_category
from app.utils.datetime import utcnow_aware


class VisitorService:
    """Create, resolve, and enrich public support visitors safely."""

    SESSION_TIMEOUT_MINUTES = 30

    def __init__(self, db: Session):
        self.db = db

    def _generate_public_token(self) -> str:
        return secrets.token_urlsafe(32)

    def _normalize_entry_metadata(self, metadata: dict[str, Any] | None) -> dict[str, Any]:
        return dict(metadata or {})

    def _derive_device_type(self, user_agent: str | None) -> DeviceType | None:
        if not user_agent:
            return None
        lowered = user_agent.lower()
        if "tablet" in lowered or "ipad" in lowered:
            return DeviceType.TABLET
        if "mobile" in lowered or "android" in lowered or "iphone" in lowered:
            return DeviceType.MOBILE
        return DeviceType.DESKTOP

    def get_public_business_profile(self, public_slug: str) -> PublicBusinessProfileResponse | None:
        """Resolve a public business profile by tenant slug."""
        tenant = self.db.execute(
            select(Tenant).where(Tenant.public_slug == public_slug.strip().lower())
        ).scalar_one_or_none()

        if not tenant:
            return None

        return PublicBusinessProfileResponse(
            tenant_id=tenant.id,
            public_slug=tenant.public_slug or "",
            display_name=tenant.display_name,
            description=tenant.description,
            long_description=tenant.long_description,
            support_headline=tenant.support_headline,
            support_subheadline=tenant.support_subheadline,
            support_hours=tenant.support_hours,
            support_response_time=tenant.support_response_time,
            support_footer_note=tenant.support_footer_note,
            widget_primary_color=tenant.widget_primary_color,
            widget_accent_color=tenant.widget_accent_color,
            widget_logo_url=tenant.widget_logo_url,
            widget_greeting_message=tenant.widget_greeting_message,
            collect_name_before_chat=bool(
                getattr(tenant.ai_config, "collect_name_before_chat", False)
            ),
            collect_email_before_chat=bool(
                getattr(tenant.ai_config, "collect_email_before_chat", False)
            ),
        )

    def list_public_businesses(
        self,
        *,
        query: str | None = None,
        page: int = 1,
        page_size: int = 24,
        category: TenantCategory | None = None,
    ) -> PublicBusinessDirectoryResponse:
        """Return publicly listed businesses for customer search and discovery."""
        thirty_days_ago = datetime.now(UTC) - timedelta(days=30)

        normalized_query = (query or "").strip()
        normalized_page = max(1, page)
        normalized_page_size = max(1, min(page_size, 100))

        base_filters = [
            Tenant.is_publicly_listed.is_(True),
            Tenant.public_slug.is_not(None),
            Tenant.status == TenantStatus.ACTIVE,
        ]
        if category is not None:
            base_filters.append(Tenant.category == category)
        if normalized_query:
            search = f"%{normalized_query.lower()}%"
            base_filters.append(
                or_(
                    func.lower(Tenant.display_name).like(search),
                    func.lower(func.coalesce(Tenant.description, "")).like(search),
                    func.lower(func.coalesce(Tenant.support_headline, "")).like(search),
                    func.lower(func.coalesce(Tenant.support_subheadline, "")).like(search),
                )
            )

        # Portable CASE expression: 1 for tenants created within the last 30 days, else 0.
        is_recent = case(
            (Tenant.created_at >= thirty_days_ago, 1),
            else_=0,
        )

        count_stmt = select(func.count()).select_from(
            select(Tenant).where(*base_filters).subquery()
        )
        total = self.db.execute(count_stmt).scalar_one()

        page_stmt = (
            select(Tenant)
            .where(*base_filters)
            .order_by(
                desc(Tenant.is_featured),
                desc(is_recent),
                asc(Tenant.display_name),
            )
            .offset((normalized_page - 1) * normalized_page_size)
            .limit(normalized_page_size)
        )
        tenants = list(self.db.execute(page_stmt).scalars())

        return PublicBusinessDirectoryResponse(
            businesses=[
                PublicBusinessDirectoryItem(
                    tenant_id=tenant.id,
                    public_slug=tenant.public_slug or "",
                    display_name=tenant.display_name,
                    description=tenant.description,
                    support_headline=tenant.support_headline,
                    support_subheadline=tenant.support_subheadline,
                    widget_logo_url=tenant.widget_logo_url,
                    is_featured=tenant.is_featured,
                    category=coerce_tenant_category(tenant.category),
                    created_at=tenant.created_at,
                )
                for tenant in tenants
            ],
            total=total,
            query=normalized_query or None,
            page=normalized_page,
            page_size=normalized_page_size,
            total_pages=ceil(total / normalized_page_size) if total else 0,
        )

    def _resolve_tenant(self, payload: VisitorBootstrapRequest) -> Tenant | None:
        if payload.workspace_id:
            return self.db.execute(
                select(Tenant).where(Tenant.id == payload.workspace_id)
            ).scalar_one_or_none()
        if payload.workspace_slug:
            return self.db.execute(
                select(Tenant).where(Tenant.public_slug == payload.workspace_slug.strip().lower())
            ).scalar_one_or_none()
        return None

    def get_visitor_by_token(self, tenant_id: UUID, visitor_token: str) -> Visitor | None:
        """Resolve a visitor by tenant and public token."""
        return self.db.execute(
            select(Visitor).where(
                Visitor.tenant_id == tenant_id,
                Visitor.public_token == visitor_token,
            )
        ).scalar_one_or_none()

    def get_visitor_session_by_token(
        self,
        visitor_id: UUID,
        session_token: str,
    ) -> VisitorSession | None:
        """Resolve a visitor session by visitor and public token."""
        return self.db.execute(
            select(VisitorSession).where(
                VisitorSession.visitor_id == visitor_id,
                VisitorSession.session_token == session_token,
            )
        ).scalar_one_or_none()

    def _create_visitor(
        self,
        tenant_id: UUID,
        entry_point: VisitorSource,
        metadata: dict[str, Any],
    ) -> Visitor:
        now = utcnow_aware()
        visitor = Visitor(
            tenant_id=tenant_id,
            public_token=self._generate_public_token(),
            first_seen_at=now,
            last_seen_at=now,
            created_from=entry_point,
            metadata_json=metadata or None,
        )
        self.db.add(visitor)
        self.db.flush()
        return visitor

    def _create_visitor_session(
        self,
        visitor: Visitor,
        entry_point: VisitorSource,
        metadata: dict[str, Any],
    ) -> VisitorSession:
        now = utcnow_aware()
        user_agent = str(metadata.get("user_agent")) if metadata.get("user_agent") else None
        visitor_session = VisitorSession(
            visitor_id=visitor.id,
            tenant_id=visitor.tenant_id,
            session_token=self._generate_public_token(),
            started_at=now,
            last_activity_at=now,
            entry_point=entry_point,
            user_agent=user_agent,
            ip_address=str(metadata.get("ip_address")) if metadata.get("ip_address") else None,
            ip_hash=(
                hash_for_lookup(str(metadata.get("ip_address")))
                if metadata.get("ip_address")
                else None
            ),
            device_type=self._derive_device_type(user_agent),
            browser_name=(
                str(metadata.get("browser_name")) if metadata.get("browser_name") else None
            ),
            os_name=str(metadata.get("os_name")) if metadata.get("os_name") else None,
            referrer_url=(
                str(metadata.get("referrer_url")) if metadata.get("referrer_url") else None
            ),
            landing_url=str(metadata.get("landing_url")) if metadata.get("landing_url") else None,
            locale=str(metadata.get("locale")) if metadata.get("locale") else None,
            created_at=now,
        )
        self.db.add(visitor_session)
        self.db.flush()
        return visitor_session

    def _resolve_or_create_visitor(
        self,
        tenant_id: UUID,
        visitor_token: str | None,
        entry_point: VisitorSource,
        metadata: dict[str, Any],
    ) -> Visitor:
        visitor = None
        if visitor_token:
            visitor = self.db.execute(
                select(Visitor).where(
                    Visitor.tenant_id == tenant_id,
                    Visitor.public_token == visitor_token,
                )
            ).scalar_one_or_none()

        if not visitor:
            visitor = self._create_visitor(tenant_id, entry_point, metadata)

        visitor.last_seen_at = utcnow_aware()
        if metadata and not visitor.metadata_json:
            visitor.metadata_json = metadata
        if payload_name := metadata.get("contact_name"):
            visitor.name = str(payload_name).strip()
        if payload_email := metadata.get("contact_email"):
            email_val = str(payload_email).strip().lower()
            visitor.email = email_val
            visitor.email_hash = hash_for_lookup(email_val)
        return visitor

    def _resolve_or_create_visitor_session(
        self,
        visitor: Visitor,
        session_token: str | None,
        entry_point: VisitorSource,
        metadata: dict[str, Any],
    ) -> VisitorSession:
        visitor_session = None
        if session_token:
            visitor_session = self.db.execute(
                select(VisitorSession).where(
                    VisitorSession.visitor_id == visitor.id,
                    VisitorSession.session_token == session_token,
                )
            ).scalar_one_or_none()

        now = utcnow_aware()
        if visitor_session:
            cutoff = now - timedelta(minutes=self.SESSION_TIMEOUT_MINUTES)
            if visitor_session.ended_at or visitor_session.last_activity_at < cutoff:
                visitor_session.ended_at = visitor_session.last_activity_at
                visitor_session = None

        if not visitor_session:
            visitor_session = self._create_visitor_session(visitor, entry_point, metadata)

        visitor_session.last_activity_at = now
        visitor_session.entry_point = entry_point
        if metadata.get("landing_url"):
            visitor_session.landing_url = str(metadata["landing_url"])
        if metadata.get("referrer_url"):
            visitor_session.referrer_url = str(metadata["referrer_url"])
        if metadata.get("locale"):
            visitor_session.locale = str(metadata["locale"])
        return visitor_session

    def bootstrap_visitor(self, payload: VisitorBootstrapRequest) -> VisitorBootstrapResponse:
        """Resolve tenant, visitor, and visitor session for public support entry."""
        tenant = self._resolve_tenant(payload)
        if not tenant or not tenant.public_slug:
            raise ValueError("Tenant not found")

        metadata = self._normalize_entry_metadata(payload.metadata)
        if payload.name:
            metadata["contact_name"] = payload.name.strip()
        if payload.email:
            metadata["contact_email"] = str(payload.email).strip().lower()
        visitor = self._resolve_or_create_visitor(
            tenant_id=tenant.id,
            visitor_token=payload.visitor_token,
            entry_point=payload.entry_point,
            metadata=metadata,
        )
        if visitor.is_blocked:
            raise PermissionError("Visitor is blocked")

        visitor_session = self._resolve_or_create_visitor_session(
            visitor=visitor,
            session_token=payload.session_token,
            entry_point=payload.entry_point,
            metadata=metadata,
        )

        self.db.commit()
        self.db.refresh(visitor)
        self.db.refresh(visitor_session)

        latest_conversation = self._get_latest_conversation_summary(visitor.id, tenant.id)

        return VisitorBootstrapResponse(
            tenant=PublicBusinessProfileResponse(
                tenant_id=tenant.id,
                public_slug=tenant.public_slug,
                display_name=tenant.display_name,
                description=tenant.description,
                long_description=tenant.long_description,
                support_headline=tenant.support_headline,
                support_subheadline=tenant.support_subheadline,
                support_hours=tenant.support_hours,
                support_response_time=tenant.support_response_time,
                support_footer_note=tenant.support_footer_note,
                widget_primary_color=tenant.widget_primary_color,
                widget_accent_color=tenant.widget_accent_color,
                widget_logo_url=tenant.widget_logo_url,
                widget_greeting_message=tenant.widget_greeting_message,
                collect_name_before_chat=bool(
                    getattr(tenant.ai_config, "collect_name_before_chat", False)
                ),
                collect_email_before_chat=bool(
                    getattr(tenant.ai_config, "collect_email_before_chat", False)
                ),
            ),
            visitor_token=visitor.public_token,
            session_token=visitor_session.session_token,
            visitor=VisitorProfileSummary(
                name=visitor.name,
                email=visitor.email,
                phone=visitor.phone,
                is_blocked=visitor.is_blocked,
                first_seen_at=visitor.first_seen_at,
                last_seen_at=visitor.last_seen_at,
            ),
            last_conversation=latest_conversation,
        )

    def _get_latest_conversation_summary(
        self,
        visitor_id: UUID,
        tenant_id: UUID,
    ) -> VisitorConversationSummary | None:
        """Return the latest visitor-linked conversation and recent transcript."""
        from app.db.models.conversation import ConversationSession

        session = self.db.execute(
            select(ConversationSession)
            .where(
                ConversationSession.tenant_id == tenant_id,
                ConversationSession.visitor_id == visitor_id,
                ConversationSession.status.in_(
                    [
                        SessionStatus.ACTIVE,
                        SessionStatus.ESCALATED,
                        SessionStatus.RESOLVED,
                        SessionStatus.ABANDONED,
                    ]
                ),
            )
            .order_by(ConversationSession.last_message_at.desc())
        ).scalar_one_or_none()

        if not session:
            return None

        messages = list(
            self.db.execute(
                select(Message)
                .where(Message.session_id == session.id)
                .order_by(Message.created_at)
                .limit(50)
            ).scalars()
        )

        return VisitorConversationSummary(
            session_id=session.id,
            status=session.status,
            last_message_at=session.last_message_at,
            messages=[
                VisitorConversationMessageSummary(
                    id=message.id,
                    role=message.role,
                    content=message.content,
                    created_at=message.created_at,
                    meta=message.meta,
                )
                for message in messages
            ],
        )

    def update_visitor_from_session(
        self,
        conversation_session_id: UUID,
        update: VisitorEnrichmentUpdate,
    ) -> None:
        """Update the linked visitor for a conversation session if one exists."""
        from app.db.models.conversation import ConversationSession

        session = self.db.execute(
            select(ConversationSession).where(ConversationSession.id == conversation_session_id)
        ).scalar_one_or_none()
        if not session or not session.visitor_id:
            return

        visitor = self.db.execute(
            select(Visitor).where(Visitor.id == session.visitor_id)
        ).scalar_one_or_none()
        if not visitor:
            return

        payload = update.model_dump(exclude_unset=True)
        changed = False
        for field, value in payload.items():
            if value and getattr(visitor, field) != value:
                setattr(visitor, field, value)
                changed = True

        if changed:
            visitor.last_seen_at = utcnow_aware()
            self.db.commit()

    def get_visitor_for_session(self, conversation_session_id: UUID):
        """Return the linked visitor model for a conversation session when present."""
        from app.db.models.conversation import ConversationSession

        session = self.db.execute(
            select(ConversationSession).where(ConversationSession.id == conversation_session_id)
        ).scalar_one_or_none()
        if not session or not session.visitor_id:
            return None

        return self.db.execute(
            select(Visitor).where(Visitor.id == session.visitor_id)
        ).scalar_one_or_none()
