"""Flow template service for managing and instantiating flow templates."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.flow import Flow, FlowStatus, FlowTemplate, FlowTriggerType
from app.db.models.tenant import Tenant
from app.schemas.flow import FlowResponse, FlowTemplateResponse


class FlowTemplateService:
    """Service for flow template management and instantiation."""

    # Tier hierarchy for tier comparison
    TIER_ORDER = {
        "trial": 0,
        "lite": 1,
        "professional": 2,
        "growth": 3,
        "enterprise": 4,
    }

    def __init__(self, db: Session):
        """
        Initialize flow template service with database session.

        Args:
            db: SQLAlchemy database session
        """
        self.db = db

    def get_templates(
        self,
        category: str | None = None,
        is_active: bool = True,
    ) -> list[FlowTemplateResponse]:
        """
        Get list of flow templates with optional filtering.

        Args:
            category: Optional category filter (e.g., 'support', 'sales', 'onboarding')
            is_active: Filter by active status (default: True)

        Returns:
            List of flow template responses
        """
        # Build query
        stmt = select(FlowTemplate)

        # Apply filters
        if is_active is not None:
            stmt = stmt.where(FlowTemplate.is_active == is_active)

        if category:
            stmt = stmt.where(FlowTemplate.category == category)

        # Order by category and name
        stmt = stmt.order_by(FlowTemplate.category, FlowTemplate.name)

        # Execute query
        templates = self.db.execute(stmt).scalars().all()

        return [FlowTemplateResponse.model_validate(t) for t in templates]

    def get_template(self, template_id: UUID) -> FlowTemplateResponse | None:
        """
        Get a single flow template by ID.

        Args:
            template_id: Template UUID

        Returns:
            Flow template response if found, None otherwise
        """
        stmt = select(FlowTemplate).where(FlowTemplate.id == template_id)
        template = self.db.execute(stmt).scalar_one_or_none()

        if not template:
            return None

        return FlowTemplateResponse.model_validate(template)

    def create_flow_from_template(
        self,
        template_id: UUID,
        tenant_id: UUID,
        name: str,
        description: str | None = None,
        created_by: UUID | None = None,
    ) -> FlowResponse:
        """
        Create a new flow instance from a template.

        This method instantiates a flow from a template, copying the template's
        nodes and trigger configuration. It also validates that the tenant's
        subscription tier meets the template's minimum tier requirement.

        Args:
            template_id: Template UUID to instantiate from
            tenant_id: Tenant UUID that will own the new flow
            name: Name for the new flow
            description: Optional custom description (uses template description if not provided)
            created_by: Optional user UUID who is creating the flow

        Returns:
            Created flow response

        Raises:
            ValueError: If template not found, tenant not found, or tier requirement not met
        """
        # Get template
        stmt = select(FlowTemplate).where(FlowTemplate.id == template_id)
        template = self.db.execute(stmt).scalar_one_or_none()

        if not template:
            raise ValueError(f"Flow template with ID {template_id} not found")

        if not template.is_active:
            raise ValueError("Cannot instantiate from inactive template")

        # Get tenant to check subscription tier
        tenant_stmt = select(Tenant).where(Tenant.id == tenant_id)
        tenant = self.db.execute(tenant_stmt).scalar_one_or_none()

        if not tenant:
            raise ValueError(f"Tenant with ID {tenant_id} not found")

        # Check tier requirement
        self._validate_tier_requirement(tenant.subscription_tier, template.min_tier)

        # Determine trigger type from trigger config
        trigger_type = self._determine_trigger_type(template.trigger_config)

        # Create flow from template
        db_flow = Flow(
            tenant_id=tenant_id,
            name=name,
            description=description or template.description,
            status=FlowStatus.DRAFT,
            is_active=False,  # Start as inactive, user must activate manually
            trigger_type=trigger_type,
            trigger_config=template.trigger_config.copy(),  # Copy trigger config
            nodes=template.template_nodes.copy(),  # Copy template nodes
            priority=0,  # Default priority
            version=1,
            created_by=created_by,
        )

        self.db.add(db_flow)
        self.db.commit()
        self.db.refresh(db_flow)

        return FlowResponse.model_validate(db_flow)

    def _validate_tier_requirement(self, tenant_tier: str, required_tier: str) -> None:
        """
        Validate that tenant's tier meets the template's minimum tier requirement.

        Args:
            tenant_tier: Tenant's current subscription tier
            required_tier: Template's minimum required tier

        Raises:
            ValueError: If tenant tier is below required tier
        """
        tenant_tier_level = self.TIER_ORDER.get(tenant_tier.lower(), -1)
        required_tier_level = self.TIER_ORDER.get(required_tier.lower(), -1)

        if tenant_tier_level < 0:
            raise ValueError(f"Invalid tenant subscription tier: {tenant_tier}")

        if required_tier_level < 0:
            raise ValueError(f"Invalid template minimum tier: {required_tier}")

        if tenant_tier_level < required_tier_level:
            raise ValueError(
                f"This template requires {required_tier} tier or higher. "
                f"Your current tier is {tenant_tier}. Please upgrade your subscription."
            )

    def _determine_trigger_type(self, trigger_config: dict) -> FlowTriggerType:
        """
        Determine trigger type from trigger configuration.

        Args:
            trigger_config: Trigger configuration dictionary

        Returns:
            FlowTriggerType enum value
        """
        # Check for different trigger types in order of priority
        if trigger_config.get("keywords"):
            return FlowTriggerType.KEYWORD
        elif trigger_config.get("intents"):
            return FlowTriggerType.INTENT
        elif trigger_config.get("schedule"):
            return FlowTriggerType.SCHEDULE
        elif trigger_config.get("events"):
            return FlowTriggerType.EVENT
        else:
            # Default to manual if no trigger config specified
            return FlowTriggerType.MANUAL
