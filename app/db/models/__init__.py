"""Database models package."""

from app.db.base import Base
from app.db.models.api_key import APIKey, APIKeyType
from app.db.models.automation import (
    ActionAuthType,
    ActionExecution,
    ActionExecutionStatus,
    ActionTriggerSource,
    ActionSecret,
    TenantAutomationConfig,
    TenantAction,
    TenantActionStatus,
    TenantActionType,
)
from app.db.models.audit import (
    AnalyticsMetric,
    AuditEntry,
    AuditEntryKind,
    AuditEntryStatus,
    AuditLog,
    AuditReport,
    AuditScope,
    AuditStatus,
)
from app.db.models.conversation import (
    ConversationSession,
    Message,
    MessageRole,
    SessionStatus,
    Ticket,
    TicketPriority,
    TicketStatus,
)
from app.db.models.customer_engagement import (
    ChannelPostStatus,
    CustomerBusinessFollow,
    TenantChannelPost,
)
from app.db.models.customer_user import CustomerUser
from app.db.models.email_notification import EmailNotification
from app.db.models.business_profile import BusinessProfile, BusinessProfileSource
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
from app.db.models.platform_email_campaign import (
    PlatformEmailCampaign,
    PlatformEmailCampaignRecipient,
)
from app.db.models.promotional_grant import AutoSignupGrantConfig, PromotionalGrant
from app.db.models.review import FeedbackCategory, PlatformFeedback, SessionRating
from app.db.models.subscription import SubscriptionHistory
from app.db.models.subscription_plan import SubscriptionPlan
from app.db.models.lead import Lead, LeadStatus
from app.db.models.sales_agent_config import TenantSalesAgentConfig
from app.db.models.visit_analytics import DailyVisit

# Import all models to ensure they're registered with SQLAlchemy
from app.db.models.tenant import Tenant, TenantAIConfig, TenantCapabilities, TenantStatus
from app.db.models.user import User, UserRole
from app.db.models.visitor import DeviceType, Visitor, VisitorSession, VisitorSource

__all__ = [
    "Base",
    # Tenant models
    "Tenant",
    "TenantAIConfig",
    "TenantCapabilities",
    "TenantStatus",
    "Visitor",
    "VisitorSession",
    "VisitorSource",
    "DeviceType",
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
    "CustomerUser",
    "CustomerBusinessFollow",
    "TenantChannelPost",
    "ChannelPostStatus",
    # Platform admin models
    "PlatformAdmin",
    "PlatformAdminRole",
    "PlatformEmailCampaign",
    "PlatformEmailCampaignRecipient",
    # API Key models
    "APIKey",
    "APIKeyType",
    # Automation models
    "TenantAction",
    "TenantActionStatus",
    "TenantActionType",
    "ActionAuthType",
    "ActionExecution",
    "ActionExecutionStatus",
    "ActionTriggerSource",
    "ActionSecret",
    "TenantAutomationConfig",
    # Audit models
    "AuditLog",
    "AnalyticsMetric",
    # Auditor staging models
    "AuditReport",
    "AuditEntry",
    "AuditScope",
    "AuditStatus",
    "AuditEntryStatus",
    "AuditEntryKind",
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
    # Promotional grant models
    "PromotionalGrant",
    "AutoSignupGrantConfig",
    # Visit analytics models
    "DailyVisit",
    # Email notification models
    "EmailNotification",
    # Lead models
    "Lead",
    "LeadStatus",
    # Sales agent config
    "TenantSalesAgentConfig",
    # Auditor models
    "BusinessProfile",
    "BusinessProfileSource",
    # Flow models
    "Flow",
    "FlowExecution",
    "FlowTemplate",
    "FlowStatus",
    "FlowTriggerType",
    "FlowExecutionStatus",
]
