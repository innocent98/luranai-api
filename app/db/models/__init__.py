"""Database models package."""

from app.db.base import Base
from app.db.models.api_key import APIKey, APIKeyType
from app.db.models.audit import AnalyticsMetric, AuditLog
from app.db.models.conversation import (
    ConversationSession,
    Message,
    MessageRole,
    SessionStatus,
    Ticket,
    TicketPriority,
    TicketStatus,
)
from app.db.models.email_notification import EmailNotification
from app.db.models.flow import (
    Flow,
    FlowExecution,
    FlowExecutionStatus,
    FlowStatus,
    FlowTemplate,
    FlowTriggerType,
)
from app.db.models.job import JobStatus, JobType, ProcessingJob
from app.db.models.knowledge import (
    ChunkVisibility,
    KnowledgeChunk,
    KnowledgeSource,
    SourceStatus,
    SourceType,
)
from app.db.models.payment import PaymentTransaction
from app.db.models.platform_admin import PlatformAdmin, PlatformAdminRole
from app.db.models.review import FeedbackCategory, PlatformFeedback, SessionRating
from app.db.models.subscription import SubscriptionHistory
from app.db.models.subscription_plan import SubscriptionPlan

# Import all models to ensure they're registered with SQLAlchemy
from app.db.models.tenant import Tenant, TenantAIConfig, TenantCapabilities, TenantStatus
from app.db.models.user import User, UserRole

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
    # API Key models
    "APIKey",
    "APIKeyType",
    # Audit models
    "AuditLog",
    "AnalyticsMetric",
    # Job models
    "ProcessingJob",
    "JobStatus",
    "JobType",
    # Review models
    "SessionRating",
    "PlatformFeedback",
    "FeedbackCategory",
    # Payment models
    "PaymentTransaction",
    # Subscription models
    "SubscriptionHistory",
    "SubscriptionPlan",
    # Email notification models
    "EmailNotification",
    # Flow models
    "Flow",
    "FlowExecution",
    "FlowTemplate",
    "FlowStatus",
    "FlowTriggerType",
    "FlowExecutionStatus",
]
