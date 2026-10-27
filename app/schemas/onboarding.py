"""Activation checklist returned by GET /api/v1/tenant/onboarding."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field


class KnowledgeDetail(BaseModel):
    sources_active: int
    sources_processing: int
    sources_failed: int


class WidgetDetail(BaseModel):
    first_seen_at: datetime | None
    last_seen_at: datetime | None
    last_seen_host: str | None


class TestConversationDetail(BaseModel):
    conversations: int


class KnowledgeStep(BaseModel):
    key: Literal["knowledge"] = "knowledge"
    done: bool
    detail: KnowledgeDetail


class WidgetStep(BaseModel):
    key: Literal["widget"] = "widget"
    done: bool
    detail: WidgetDetail


class TestConversationStep(BaseModel):
    key: Literal["test_conversation"] = "test_conversation"
    done: bool
    detail: TestConversationDetail


OnboardingStep = Annotated[
    KnowledgeStep | WidgetStep | TestConversationStep, Field(discriminator="key")
]


class OnboardingAI(BaseModel):
    first_responder: Literal["ai", "ai_with_handoff", "human"]
    will_answer: bool
    blocker: Literal["no_knowledge", "knowledge_not_indexed", "human_first"] | None


class OnboardingStatus(BaseModel):
    steps: list[OnboardingStep]
    ai: OnboardingAI
    completed: int
    total: int
