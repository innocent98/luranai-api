"""N3 — who answers first: constants and helpers (spec 2026-10-10)."""

from typing import Literal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models.knowledge import KnowledgeSource, SourceStatus

FIRST_RESPONDER_AI = "ai"
FIRST_RESPONDER_HANDOFF = "ai_with_handoff"
FIRST_RESPONDER_HUMAN = "human"
FirstResponder = Literal["ai", "ai_with_handoff", "human"]

HUMAN_FIRST_REASON = "Human-only mode"
HUMAN_FIRST_REPLY = "Thanks for reaching out — connecting you with someone from our team now."
HUMAN_FIRST_WELCOME = (
    "Hi! Welcome to {company_name}. Send us a message and someone from our team will reply."
)
MISSED_NOTICE = "Our team couldn't get to you right now. We'll reply right here as soon as we can."


def count_active_knowledge_sources(db: Session, tenant_id: UUID) -> int:
    """ACTIVE sources only; PROCESSING/INACTIVE/FAILED cannot answer questions."""
    return (
        db.execute(
            select(func.count())
            .select_from(KnowledgeSource)
            .where(
                KnowledgeSource.tenant_id == tenant_id,
                KnowledgeSource.status == SourceStatus.ACTIVE,
            )
        ).scalar()
        or 0
    )


def default_first_responder(db: Session, tenant_id: UUID) -> str:
    if count_active_knowledge_sources(db, tenant_id) == 0:
        return FIRST_RESPONDER_HUMAN
    return FIRST_RESPONDER_AI


def apply_first_responder_preset(update_data: dict) -> dict:
    """ai_with_handoff writes escalate into both grounding actions (spec §4.4)."""
    out = dict(update_data)
    if out.get("first_responder") == FIRST_RESPONDER_HANDOFF:
        out["no_retrieval_action"] = "escalate"
        out["low_confidence_action"] = "escalate"
    return out
