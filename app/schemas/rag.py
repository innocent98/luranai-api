"""RAG service schemas."""

from typing import Optional

from pydantic import BaseModel, Field

from app.db.models.conversation import EscalationAction


class EscalationDecision(BaseModel):
    """Decision on whether to escalate conversation."""

    action: EscalationAction = Field(..., description="Escalation action to take")
    reason: Optional[str] = Field(None, description="Reason for escalation decision")
    skip_quality_check: bool = Field(
        default=False,
        description="If True, skip quality evaluation (e.g., user satisfaction detected)"
    )


class ResponseQuality(BaseModel):
    """Evaluation of AI response quality."""

    is_substantial: bool = Field(..., description="Response is substantial and helpful")
    has_uncertainty: bool = Field(..., description="AI expressed uncertainty")
    word_count: int = Field(..., description="Number of words in response")
    char_count: int = Field(..., description="Number of characters in response")


class PromptComponents(BaseModel):
    """Assembled prompt components for AI generation."""

    full_prompt: str = Field(..., description="Complete assembled prompt")
    knowledge_chunks_used: int = Field(..., description="Number of knowledge chunks included")
    history_turns_used: int = Field(..., description="Number of conversation turns included")
