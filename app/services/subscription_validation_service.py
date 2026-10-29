"""Subscription validation service for tier changes."""

from datetime import UTC
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models.automation import TenantAction
from app.db.models.flow import Flow, FlowStatus
from app.db.models.knowledge import KnowledgeSource, SourceStatus
from app.db.models.sales_agent_config import TenantSalesAgentConfig
from app.db.models.user import User
from app.db.models.whatsapp import WhatsAppConnection
from app.services.knowledge_service import KnowledgeService
from app.services.plan_service import PlanService


class ValidationViolation(BaseModel):
    """A limit that would be exceeded by tier change."""

    field: str
    current_value: int
    allowed_value: int
    difference: int
    action_required: str


class ValidationResult(BaseModel):
    """Result of tier change validation."""

    valid: bool
    violations: list[ValidationViolation] = []
    warnings: list[str] = []


class SubscriptionValidationService:
    """Service for validating tenant data against tier limits."""

    def __init__(self, db: Session):
        """Initialize validation service with database session."""
        self.db = db
        self.plan_service = PlanService(db)
        self.knowledge_service = KnowledgeService(db)

    def validate_tier_change(self, tenant_id: UUID, new_tier: str) -> ValidationResult:
        """
        Validate if tenant data fits within new tier limits.

        Args:
            tenant_id: Tenant UUID
            new_tier: Target tier (lite, professional, etc.)

        Returns:
            ValidationResult with violations and warnings
        """
        new_limits = self.plan_service.get_plan_config(new_tier)
        violations = []
        warnings = []

        # Check each limit
        if violation := self._check_knowledge_sources(tenant_id, new_limits):
            violations.append(violation)

        if violation := self._check_storage_usage(tenant_id, new_limits):
            violations.append(violation)

        if violation := self._check_team_members(tenant_id, new_limits):
            violations.append(violation)

        if violation := self._check_url_sources(tenant_id, new_limits):
            violations.append(violation)

        if violation := self._check_flows(tenant_id, new_limits):
            violations.append(violation)

        if violation := self._check_automation_actions(tenant_id, new_limits):
            violations.append(violation)

        if violation := self._check_sales_close(tenant_id, new_limits):
            violations.append(violation)

        if violation := self._check_crm_notifications(tenant_id, new_limits):
            violations.append(violation)

        if violation := self._check_whatsapp_numbers(tenant_id, new_limits):
            violations.append(violation)

        if warning := self._check_conversations(tenant_id, new_limits):
            warnings.append(warning)

        return ValidationResult(
            valid=len(violations) == 0,
            violations=violations,
            warnings=warnings,
        )

    def _check_knowledge_sources(
        self, tenant_id: UUID, new_limits: dict
    ) -> ValidationViolation | None:
        """
        Check if knowledge sources count exceeds new tier limit.

        Args:
            tenant_id: Tenant UUID
            new_limits: New tier capabilities dict

        Returns:
            ValidationViolation if limit exceeded, None otherwise
        """
        max_sources = new_limits.get("max_knowledge_sources")

        # If unlimited (None), no violation
        if max_sources is None:
            return None

        # Count active sources (same logic as check_knowledge_source_limit)
        current_count = (
            self.db.query(KnowledgeSource)
            .filter(
                KnowledgeSource.tenant_id == tenant_id,
                KnowledgeSource.status == SourceStatus.ACTIVE,
            )
            .count()
        )

        if current_count > max_sources:
            difference = current_count - max_sources
            return ValidationViolation(
                field="knowledge_sources",
                current_value=current_count,
                allowed_value=max_sources,
                difference=difference,
                action_required=f"Delete {difference} knowledge source{'s' if difference > 1 else ''} before downgrading",
            )

        return None

    def _check_storage_usage(self, tenant_id: UUID, new_limits: dict) -> ValidationViolation | None:
        """
        Check if storage usage exceeds new tier limit.

        Args:
            tenant_id: Tenant UUID
            new_limits: New tier capabilities dict

        Returns:
            ValidationViolation if limit exceeded, None otherwise
        """
        max_storage_mb = new_limits.get("max_knowledge_mb")

        # If unlimited (None), no violation
        if max_storage_mb is None:
            return None

        # Calculate current storage (includes ACTIVE + PROCESSING)
        current_storage_mb = self.knowledge_service.calculate_tenant_storage_mb(tenant_id)

        if current_storage_mb > max_storage_mb:
            difference_mb = int(current_storage_mb - max_storage_mb)
            return ValidationViolation(
                field="storage_usage",
                current_value=int(current_storage_mb),
                allowed_value=max_storage_mb,
                difference=difference_mb,
                action_required=f"Reduce storage by {difference_mb} MB (delete files or wait for processing to complete)",
            )

        return None

    def _check_team_members(self, tenant_id: UUID, new_limits: dict) -> ValidationViolation | None:
        """
        Check if team member count exceeds new tier limit.

        Args:
            tenant_id: Tenant UUID
            new_limits: New tier capabilities dict

        Returns:
            ValidationViolation if limit exceeded, None otherwise
        """
        max_team_members = new_limits.get("max_team_members")

        # If unlimited (None), no violation
        if max_team_members is None:
            return None

        # Count active users
        current_count = (
            self.db.query(User)
            .filter(
                User.tenant_id == tenant_id,
                User.is_active.is_(True),
            )
            .count()
        )

        if current_count > max_team_members:
            difference = current_count - max_team_members
            return ValidationViolation(
                field="team_members",
                current_value=current_count,
                allowed_value=max_team_members,
                difference=difference,
                action_required=f"Remove {difference} team member{'s' if difference > 1 else ''} before downgrading",
            )

        return None

    def _check_url_sources(self, tenant_id: UUID, new_limits: dict) -> ValidationViolation | None:
        """
        Check if tenant has URL sources when new tier doesn't allow them.

        Args:
            tenant_id: Tenant UUID
            new_limits: New tier capabilities dict

        Returns:
            ValidationViolation if URLs exist but not allowed, None otherwise
        """
        can_use_urls = new_limits.get("can_use_url_sources", False)

        # If URLs allowed in new tier, no violation
        if can_use_urls:
            return None

        # Count active URL sources
        url_count = (
            self.db.query(KnowledgeSource)
            .filter(
                KnowledgeSource.tenant_id == tenant_id,
                KnowledgeSource.status == SourceStatus.ACTIVE,
                KnowledgeSource.source_category == "url",
            )
            .count()
        )

        if url_count > 0:
            return ValidationViolation(
                field="url_sources",
                current_value=url_count,
                allowed_value=0,
                difference=url_count,
                action_required=f"Delete {url_count} URL source{'s' if url_count > 1 else ''} (not supported in this tier)",
            )

        return None

    def _check_flows(self, tenant_id: UUID, new_limits: dict) -> ValidationViolation | None:
        """Check if current flow count exceeds the target tier limit."""
        max_flows = new_limits.get("max_flows")

        if max_flows is None:
            return None

        current_count = (
            self.db.query(Flow)
            .filter(
                Flow.tenant_id == tenant_id,
                Flow.status != FlowStatus.ARCHIVED,
            )
            .count()
        )

        if current_count > max_flows:
            difference = current_count - max_flows
            return ValidationViolation(
                field="flows",
                current_value=current_count,
                allowed_value=max_flows,
                difference=difference,
                action_required=f"Archive {difference} flow{'s' if difference > 1 else ''} before downgrading",
            )

        return None

    def _check_automation_actions(
        self, tenant_id: UUID, new_limits: dict
    ) -> ValidationViolation | None:
        """Check if current automation action count exceeds the target tier limit."""
        max_actions = new_limits.get("max_automation_actions")

        if max_actions is None:
            return None

        current_count = (
            self.db.query(TenantAction).filter(TenantAction.tenant_id == tenant_id).count()
        )

        if current_count > max_actions:
            difference = current_count - max_actions
            return ValidationViolation(
                field="automation_actions",
                current_value=current_count,
                allowed_value=max_actions,
                difference=difference,
                action_required=(
                    f"Delete {difference} automation action{'s' if difference > 1 else ''} before downgrading"
                ),
            )

        return None

    def _sales_config(self, tenant_id: UUID) -> TenantSalesAgentConfig | None:
        return self.db.execute(
            select(TenantSalesAgentConfig).where(TenantSalesAgentConfig.tenant_id == tenant_id)
        ).scalar_one_or_none()

    def _check_sales_close(self, tenant_id: UUID, new_limits: dict) -> ValidationViolation | None:
        """Meeting booking and quotes must be switched off before moving to a plan without them."""
        if new_limits.get("can_use_sales_close", False):
            return None
        config = self._sales_config(tenant_id)
        active = (
            sum(1 for on in (config.meeting_booking_enabled, config.quotation_enabled) if on)
            if config
            else 0
        )
        if not active:
            return None
        return ValidationViolation(
            field="sales_close",
            current_value=active,
            allowed_value=0,
            difference=active,
            action_required="Turn off meeting booking and quotes in the sales agent settings before downgrading",
        )

    def _check_crm_notifications(
        self, tenant_id: UUID, new_limits: dict
    ) -> ValidationViolation | None:
        """Slack and CRM notifications must be switched off before moving to a plan without them."""
        if new_limits.get("can_use_crm_notifications", False):
            return None
        config = self._sales_config(tenant_id)
        if config is None:
            return None
        channels = config.notify_channels or {}
        active = sum(1 for name in ("slack", "crm") if channels.get(name))
        if not active:
            return None
        return ValidationViolation(
            field="crm_notifications",
            current_value=active,
            allowed_value=0,
            difference=active,
            action_required="Turn off Slack and CRM notifications in the sales agent settings before downgrading",
        )

    def _check_whatsapp_numbers(
        self, tenant_id: UUID, new_limits: dict
    ) -> ValidationViolation | None:
        """Linked WhatsApp numbers must fit the target plan's allowance (none without the flag)."""
        allowed = new_limits.get("max_whatsapp_numbers")
        if not new_limits.get("can_use_whatsapp", True):
            allowed = 0
        if allowed is None:
            return None
        current = int(
            self.db.execute(
                select(func.count())
                .select_from(WhatsAppConnection)
                .where(WhatsAppConnection.tenant_id == tenant_id)
            ).scalar_one()
        )
        if current <= allowed:
            return None
        difference = current - allowed
        return ValidationViolation(
            field="whatsapp_numbers",
            current_value=current,
            allowed_value=allowed,
            difference=difference,
            action_required=f"Disconnect {difference} WhatsApp number{'s' if difference > 1 else ''} before downgrading",
        )

    def _check_conversations(self, tenant_id: UUID, new_limits: dict) -> str | None:
        """
        Check conversation usage against new tier limit (informational only).

        Conversations reset monthly, so this is a warning, not a violation.

        Args:
            tenant_id: Tenant UUID
            new_limits: New tier capabilities dict

        Returns:
            Warning message if current usage exceeds new limit, None otherwise
        """
        from datetime import datetime

        from app.db.models.conversation import ConversationSession

        max_conversations = new_limits.get("max_conversations_per_month")

        # If unlimited (None), no warning
        if max_conversations is None:
            return None

        # Get start of current month
        now = datetime.now(UTC)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        # Count conversations this month
        monthly_count = (
            self.db.query(ConversationSession)
            .filter(
                ConversationSession.tenant_id == tenant_id,
                ConversationSession.created_at >= month_start,
            )
            .count()
        )

        if monthly_count > max_conversations:
            return (
                f"Your current monthly conversations ({monthly_count}) exceed the new tier's "
                f"limit ({max_conversations}). Limit will reset at start of next month."
            )

        return None
