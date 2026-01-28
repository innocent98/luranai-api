"""Subscription validation service for tier changes."""

from typing import Optional, List
from uuid import UUID
from sqlalchemy.orm import Session
from sqlalchemy import func
from pydantic import BaseModel
from loguru import logger

from app.db.models.knowledge import KnowledgeSource, SourceStatus
from app.db.models.user import User
from app.services.plan_service import PlanService
from app.services.knowledge_service import KnowledgeService


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
    violations: List[ValidationViolation] = []
    warnings: List[str] = []


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

        if warning := self._check_conversations(tenant_id, new_limits):
            warnings.append(warning)

        return ValidationResult(
            valid=len(violations) == 0,
            violations=violations,
            warnings=warnings,
        )

    def _check_knowledge_sources(
        self, tenant_id: UUID, new_limits: dict
    ) -> Optional[ValidationViolation]:
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
        current_count = self.db.query(KnowledgeSource).filter(
            KnowledgeSource.tenant_id == tenant_id,
            KnowledgeSource.status == SourceStatus.ACTIVE,
        ).count()

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

    def _check_storage_usage(
        self, tenant_id: UUID, new_limits: dict
    ) -> Optional[ValidationViolation]:
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

    def _check_team_members(
        self, tenant_id: UUID, new_limits: dict
    ) -> Optional[ValidationViolation]:
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
        current_count = self.db.query(User).filter(
            User.tenant_id == tenant_id,
            User.is_active == True,
        ).count()

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

    def _check_url_sources(
        self, tenant_id: UUID, new_limits: dict
    ) -> Optional[ValidationViolation]:
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
        url_count = self.db.query(KnowledgeSource).filter(
            KnowledgeSource.tenant_id == tenant_id,
            KnowledgeSource.status == SourceStatus.ACTIVE,
            KnowledgeSource.source_category == "url",
        ).count()

        if url_count > 0:
            return ValidationViolation(
                field="url_sources",
                current_value=url_count,
                allowed_value=0,
                difference=url_count,
                action_required=f"Delete {url_count} URL source{'s' if url_count > 1 else ''} (not supported in this tier)",
            )

        return None

    def _check_conversations(self, tenant_id: UUID, new_limits: dict) -> Optional[str]:
        """
        Check conversation usage against new tier limit (informational only).

        Conversations reset monthly, so this is a warning, not a violation.

        Args:
            tenant_id: Tenant UUID
            new_limits: New tier capabilities dict

        Returns:
            Warning message if current usage exceeds new limit, None otherwise
        """
        from datetime import datetime, timezone as tz
        from app.db.models.conversation import ConversationSession

        max_conversations = new_limits.get("max_conversations_per_month")

        # If unlimited (None), no warning
        if max_conversations is None:
            return None

        # Get start of current month
        now = datetime.now(tz.utc)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        # Count conversations this month
        monthly_count = self.db.query(ConversationSession).filter(
            ConversationSession.tenant_id == tenant_id,
            ConversationSession.created_at >= month_start,
        ).count()

        if monthly_count > max_conversations:
            return (
                f"Your current monthly conversations ({monthly_count}) exceed the new tier's "
                f"limit ({max_conversations}). Limit will reset at start of next month."
            )

        return None
