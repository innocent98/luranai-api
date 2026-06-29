"""TenantSalesAgentConfig — per-tenant sales agent configuration."""

import uuid

from sqlalchemy import JSON, Boolean, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDMixin

_DEFAULT_TRIGGER_INTENTS: list[str] = [
    "PRICING_INQUIRY",
    "DEMO_REQUEST",
    "PURCHASE_INTENT",
    "ENTERPRISE_PROSPECT",
]

_DEFAULT_QUALIFICATION_QUESTIONS: list[dict] = [
    {"key": "company_size", "question": "How large is your team?", "type": "text"},
    {"key": "use_case", "question": "What problem are you trying to solve?", "type": "text"},
    {"key": "timeline", "question": "What is your expected timeline to get started?", "type": "text"},
]

_DEFAULT_NOTIFY_CHANNELS: dict = {"email": True, "slack": False, "in_app": False}

_DEFAULT_AVAILABILITY: dict = {
    "weekdays": [1, 2, 3, 4, 5],
    "start": "09:00",
    "end": "17:00",
    "slot_minutes": 30,
    "buffer_minutes": 15,
    "tenant_timezone": "UTC",
}


class TenantSalesAgentConfig(Base, UUIDMixin, TimestampMixin):
    """Per-tenant configuration for the AI sales agent."""

    __tablename__ = "tenant_sales_agent_config"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        unique=True,
        index=True,
        nullable=False,
        comment="One config per tenant",
    )

    # Feature toggles
    agent_enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        comment="Master switch — enables the AI sales agent for this tenant",
    )
    lead_capture_enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        comment="Capture leads when sales intents are detected",
    )
    meeting_booking_enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        comment="Allow the agent to schedule meetings via calendar_provider",
    )
    quotation_enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        comment="Allow the agent to generate pricing quotations",
    )

    # Integration providers
    crm_provider: Mapped[str | None] = mapped_column(
        String(80),
        nullable=True,
        comment="CRM provider slug (e.g. 'hubspot', 'pipedrive')",
    )
    calendar_provider: Mapped[str | None] = mapped_column(
        String(80),
        nullable=True,
        comment="Calendar provider slug (e.g. 'google', 'calendly')",
    )

    # Notification settings
    notify_channels: Mapped[dict] = mapped_column(
        JSON,
        nullable=False,
        default=lambda: dict(_DEFAULT_NOTIFY_CHANNELS),
        comment="Notification channels: {email, slack, in_app}",
    )
    notification_email: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        comment="Email address for sales notifications (overrides tenant owner email)",
    )

    # Agent behaviour
    trigger_intents: Mapped[list] = mapped_column(
        JSON,
        nullable=False,
        default=lambda: list(_DEFAULT_TRIGGER_INTENTS),
        comment="Intent strings that activate sales capture",
    )
    qualification_questions: Mapped[list] = mapped_column(
        JSON,
        nullable=False,
        default=lambda: list(_DEFAULT_QUALIFICATION_QUESTIONS),
        comment="Ordered list of qualification question dicts",
    )
    pricing_tiers: Mapped[list] = mapped_column(
        JSON,
        nullable=False,
        default=list,
        comment="Tenant-defined pricing tier dicts for quotation generation",
    )
    currency: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
        default="NGN",
        comment="ISO 4217 currency code used in quotations (default NGN)",
    )
    availability: Mapped[dict] = mapped_column(
        JSON,
        nullable=False,
        default=lambda: dict(_DEFAULT_AVAILABILITY),
        comment="Bookable-window rules for the meeting picker",
    )

    def __repr__(self) -> str:
        return f"<TenantSalesAgentConfig(tenant_id={self.tenant_id}, agent_enabled={self.agent_enabled})>"

    @classmethod
    def default_for(cls, tenant_id: uuid.UUID) -> "TenantSalesAgentConfig":
        """Return an unpersisted instance pre-filled with production defaults."""
        return cls(
            tenant_id=tenant_id,
            agent_enabled=False,
            lead_capture_enabled=True,
            meeting_booking_enabled=False,
            quotation_enabled=False,
            notify_channels=dict(_DEFAULT_NOTIFY_CHANNELS),
            trigger_intents=list(_DEFAULT_TRIGGER_INTENTS),
            qualification_questions=list(_DEFAULT_QUALIFICATION_QUESTIONS),
            pricing_tiers=[],
            currency="NGN",
            availability=dict(_DEFAULT_AVAILABILITY),
        )
