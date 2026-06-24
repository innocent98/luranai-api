"""Lead model — full Stage 3 schema; auditor populates a subset."""

import enum
import hashlib
import uuid

from sqlalchemy import JSON, ForeignKey, Index, Integer, String, event
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.orm import Mapped, mapped_column

from app.core.encryption import EncryptedString
from app.db.base import Base, TimestampMixin, UUIDMixin


class LeadStatus(str, enum.Enum):
    NEW = "new"
    CONTACTED = "contacted"
    QUALIFIED = "qualified"
    CONVERTED = "converted"


class Lead(Base, UUIDMixin, TimestampMixin):
    """Sales lead entity. Stage 4 auditor populates a subset of fields;
    scoring/CRM/meeting/status fields exist but stay null until Stage 3."""

    __tablename__ = "leads"
    __table_args__ = (Index("ix_leads_tenant_email_hash", "tenant_id", "email_hash"),)

    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=True, index=True
    )
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    name: Mapped[str | None] = mapped_column(EncryptedString, nullable=True)
    email: Mapped[str | None] = mapped_column(EncryptedString, nullable=True)
    email_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    company: Mapped[str | None] = mapped_column(String(255), nullable=True)
    team_size: Mapped[str | None] = mapped_column(String(50), nullable=True)
    industry: Mapped[str | None] = mapped_column(String(120), nullable=True)
    intent: Mapped[str | None] = mapped_column(String(255), nullable=True)
    lead_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    score_breakdown: Mapped[list | None] = mapped_column(JSON, nullable=True)
    status: Mapped[LeadStatus] = mapped_column(
        SQLEnum(LeadStatus, name="lead_status", values_callable=lambda x: [e.value for e in x]),
        default=LeadStatus.NEW,
        nullable=False,
    )
    source: Mapped[str | None] = mapped_column(String(80), nullable=True)
    utm: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    crm_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    meeting_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    audit_report_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("audit_reports.id", ondelete="SET NULL"), nullable=True
    )

    @staticmethod
    def compute_email_hash(email: str) -> str:
        """SHA-256 hex digest of the stripped, lowercased email for deterministic lookups.

        Intentionally lowercases for canonical, case-insensitive matching — other models
        normalize email to lowercase upstream, so the hashes agree in practice.
        """
        return hashlib.sha256(email.strip().lower().encode()).hexdigest()


@event.listens_for(Lead, "before_insert")
@event.listens_for(Lead, "before_update")
def _populate_lead_email_hash(mapper, connection, target):
    """Auto-populate email_hash from email before every insert/update."""
    if target.email:
        target.email_hash = Lead.compute_email_hash(target.email)
