"""Activation checklist: has this tenant reached first value?

Everything here is scoped to a single `tenant_id` and read-only. `knowledge.done` is deliberately
stricter than "an ACTIVE source exists": the vectors must really be in the vector store, because
an ACTIVE source with no vectors answers nothing (that is how the text-upload bug looked).
"""

from uuid import UUID

from loguru import logger
from sqlalchemy import distinct, func, select
from sqlalchemy.orm import Session

from app.db.models.conversation import ConversationSession, Message, MessageRole
from app.db.models.knowledge import KnowledgeSource, SourceStatus
from app.db.models.tenant import Tenant, TenantAIConfig
from app.schemas.onboarding import (
    KnowledgeDetail,
    KnowledgeStep,
    OnboardingAI,
    OnboardingStatus,
    TestConversationDetail,
    TestConversationStep,
    WidgetDetail,
    WidgetStep,
)

TOTAL_STEPS = 3


def _source_counts(db: Session, tenant_id: UUID) -> dict[SourceStatus, int]:
    rows = db.execute(
        select(KnowledgeSource.status, func.count())
        .where(KnowledgeSource.tenant_id == tenant_id)
        .group_by(KnowledgeSource.status)
    ).all()
    return dict(rows)


def _knowledge_is_indexed(db: Session, tenant_id: UUID) -> bool:
    """True when at least one ACTIVE source has vectors the AI can retrieve."""
    from app.utils.qdrant_client import qdrant_manager

    active_ids = (
        db.execute(
            select(KnowledgeSource.id).where(
                KnowledgeSource.tenant_id == tenant_id,
                KnowledgeSource.status == SourceStatus.ACTIVE,
            )
        )
        .scalars()
        .all()
    )
    if not active_ids:
        return False
    count = qdrant_manager.count_searchable_vectors(tenant_id, list(active_ids))
    if count is None:
        # Could not check: don't claim success we can't verify.
        logger.warning(f"Onboarding: vector store unreachable for tenant {tenant_id}")
        return False
    return count > 0


def _conversations_with_customer_message(db: Session, tenant_id: UUID) -> int:
    return (
        db.execute(
            select(func.count(distinct(Message.session_id)))
            .join(ConversationSession, ConversationSession.id == Message.session_id)
            .where(
                ConversationSession.tenant_id == tenant_id,
                Message.role == MessageRole.USER,
            )
        ).scalar()
        or 0
    )


def get_onboarding_status(db: Session, tenant_id: UUID) -> OnboardingStatus:
    tenant = db.execute(select(Tenant).where(Tenant.id == tenant_id)).scalar_one()

    counts = _source_counts(db, tenant_id)
    sources_active = counts.get(SourceStatus.ACTIVE, 0)
    indexed = _knowledge_is_indexed(db, tenant_id) if sources_active else False

    knowledge = KnowledgeStep(
        done=indexed,
        detail=KnowledgeDetail(
            sources_active=sources_active,
            sources_processing=counts.get(SourceStatus.PROCESSING, 0),
            sources_failed=counts.get(SourceStatus.FAILED, 0),
        ),
    )
    widget = WidgetStep(
        done=tenant.widget_first_seen_at is not None,
        detail=WidgetDetail(
            first_seen_at=tenant.widget_first_seen_at,
            last_seen_at=tenant.widget_last_seen_at,
            last_seen_host=tenant.widget_last_seen_host,
        ),
    )
    conversations = _conversations_with_customer_message(db, tenant_id)
    test_conversation = TestConversationStep(
        done=conversations > 0, detail=TestConversationDetail(conversations=conversations)
    )

    first_responder = (
        db.execute(
            select(TenantAIConfig.first_responder).where(TenantAIConfig.tenant_id == tenant_id)
        ).scalar_one_or_none()
        or "ai"
    )
    # Precedence: nothing to answer from, then nothing the AI can find, then human-first mode.
    if sources_active == 0:
        blocker = "no_knowledge"
    elif not indexed:
        blocker = "knowledge_not_indexed"
    elif first_responder == "human":
        blocker = "human_first"
    else:
        blocker = None

    steps = [knowledge, widget, test_conversation]
    return OnboardingStatus(
        steps=steps,
        ai=OnboardingAI(
            first_responder=first_responder, will_answer=blocker is None, blocker=blocker
        ),
        completed=sum(1 for step in steps if step.done),
        total=TOTAL_STEPS,
    )
