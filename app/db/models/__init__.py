"""Database models package."""

from app.db.base import Base

# Import all models to ensure they're registered with SQLAlchemy
from app.db.models.tenant import Tenant, TenantAIConfig, TenantCapabilities, TenantStatus
from app.db.models.knowledge import (
    KnowledgeSource,
    KnowledgeChunk,
    SourceType,
    SourceStatus,
    ChunkVisibility,
)
from app.db.models.conversation import (
    ConversationSession,
    Message,
    Ticket,
    SessionStatus,
    MessageRole,
    TicketStatus,
    TicketPriority,
)
from app.db.models.user import User, UserRole
from app.db.models.platform_admin import PlatformAdmin, PlatformAdminRole
from app.db.models.audit import AuditLog, AnalyticsMetric
from app.db.models.job import ProcessingJob, JobStatus, JobType

__all__ = [
    "Base",
    # Tenant models
    "Tenant",
    "TenantAIConfig",
    "TenantCapabilities",
    "TenantStatus",
    # Knowledge models
    "KnowledgeSource",
    "KnowledgeChunk",
    "SourceType",
    "SourceStatus",
    "ChunkVisibility",
    # Conversation models
    "ConversationSession",
    "Message",
    "Ticket",
    "SessionStatus",
    "MessageRole",
    "TicketStatus",
    "TicketPriority",
    # User models
    "User",
    "UserRole",
    # Platform admin models
    "PlatformAdmin",
    "PlatformAdminRole",
    # Audit models
    "AuditLog",
    "AnalyticsMetric",
    # Job models
    "ProcessingJob",
    "JobStatus",
    "JobType",
]
