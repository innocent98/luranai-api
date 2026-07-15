"""Tests that flow_engine passes BusinessProfile to assemble_prompt."""
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.orm import Session

from app.db.models.business_profile import BusinessProfile, BusinessProfileSource
from app.db.models.flow import Flow, FlowExecution, FlowExecutionStatus, FlowStatus, FlowTriggerType
from app.services.flow_engine import FlowEngine


def _make_flow(db: Session, tenant_id: uuid.UUID) -> Flow:
    flow = Flow(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        name="test-flow",
        status=FlowStatus.ACTIVE,
        is_active=True,
        trigger_type=FlowTriggerType.MANUAL,
        trigger_config={},
        nodes={},
        priority=1,
        version=1,
    )
    db.add(flow)
    db.flush()
    return flow


@pytest.mark.asyncio
async def test_flow_engine_passes_business_profile_to_assemble_prompt(db: Session):
    """When a tenant has a BusinessProfile, flow_engine must pass it to assemble_prompt."""
    from app.db.models.conversation import ConversationSession, SessionStatus
    from app.db.models.tenant import Tenant, TenantAIConfig

    tid = uuid.uuid4()
    session_id = uuid.uuid4()

    tenant = Tenant(id=tid, name=f"t_{tid.hex[:8]}", display_name="TestCo", api_key_hash="h")
    db.add(tenant)

    ai_config = TenantAIConfig(
        id=uuid.uuid4(), tenant_id=tid,
        brand_voice={}, escalation_keywords=[], max_response_tokens=250,
        grounding_requirement=False, grounding_mode="balanced",
        low_confidence_action="clarify", no_retrieval_action="clarify",
    )
    db.add(ai_config)

    conv_session = ConversationSession(
        id=session_id, tenant_id=tid,
        user_identifier="u1", status=SessionStatus.ACTIVE, meta={},
    )
    db.add(conv_session)

    profile = BusinessProfile(
        tenant_id=tid, summary="Lagos fintech", industry="fintech",
        offerings=["wallet"], audience=[], positioning="",
        key_entities=["Co"], tone={}, source=BusinessProfileSource.AUDITOR,
    )
    db.add(profile)
    db.flush()

    flow = _make_flow(db, tid)
    execution = FlowExecution(
        id=uuid.uuid4(), flow_id=flow.id, session_id=session_id,
        status=FlowExecutionStatus.RUNNING, collected_data={}, current_node_id="n1",
    )
    db.add(execution)
    db.flush()

    node = {
        "type": "ai_action", "id": "n1",
        "config": {"prompt": "Hello {user_input}", "use_rag": False, "auto_retrieve": False},
    }

    engine = FlowEngine(db)
    captured: dict = {}

    def fake_assemble_prompt(*, business_profile=None, **kwargs):
        captured["business_profile"] = business_profile
        return SimpleNamespace(full_prompt="ok prompt", prompt_tokens=5)

    with patch.object(engine.rag_service, "assemble_prompt", side_effect=fake_assemble_prompt), \
         patch.object(engine.rag_service, "generate_ai_response", new=AsyncMock(return_value=("hi", 5, False))):
        await engine._exec_ai_action(execution, node, "hi")

    assert captured.get("business_profile") is not None
    assert captured["business_profile"].tenant_id == tid


@pytest.mark.asyncio
async def test_flow_engine_noop_when_no_business_profile(db: Session):
    """When no BusinessProfile exists, assemble_prompt receives business_profile=None."""
    from app.db.models.conversation import ConversationSession, SessionStatus
    from app.db.models.tenant import Tenant, TenantAIConfig

    tid = uuid.uuid4()
    session_id = uuid.uuid4()

    tenant = Tenant(id=tid, name=f"t_{tid.hex[:8]}", display_name="NoCo", api_key_hash="h")
    db.add(tenant)

    ai_config = TenantAIConfig(
        id=uuid.uuid4(), tenant_id=tid,
        brand_voice={}, escalation_keywords=[], max_response_tokens=250,
        grounding_requirement=False, grounding_mode="balanced",
        low_confidence_action="clarify", no_retrieval_action="clarify",
    )
    db.add(ai_config)

    conv_session = ConversationSession(
        id=session_id, tenant_id=tid,
        user_identifier="u2", status=SessionStatus.ACTIVE, meta={},
    )
    db.add(conv_session)
    db.flush()

    flow = _make_flow(db, tid)
    execution = FlowExecution(
        id=uuid.uuid4(), flow_id=flow.id, session_id=session_id,
        status=FlowExecutionStatus.RUNNING, collected_data={}, current_node_id="n1",
    )
    db.add(execution)
    db.flush()

    node = {
        "type": "ai_action", "id": "n1",
        "config": {"prompt": "Hi {user_input}", "use_rag": False, "auto_retrieve": False},
    }

    engine = FlowEngine(db)
    captured: dict = {}

    def fake_assemble_prompt(*, business_profile=None, **kwargs):
        captured["business_profile"] = business_profile
        return SimpleNamespace(full_prompt="ok prompt", prompt_tokens=5)

    with patch.object(engine.rag_service, "assemble_prompt", side_effect=fake_assemble_prompt), \
         patch.object(engine.rag_service, "generate_ai_response", new=AsyncMock(return_value=("hi", 5, False))):
        await engine._exec_ai_action(execution, node, "hi")

    assert captured.get("business_profile") is None
