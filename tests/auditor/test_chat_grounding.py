"""Tests for business profile injection into chat grounding (Task 20)."""

import uuid
from types import SimpleNamespace

from app.db.models.business_profile import BusinessProfile, BusinessProfileSource
from app.services.rag_service import RAGService


def _ai_config():
    return SimpleNamespace(
        brand_voice=None, allowed_topics=None, blocked_topics=None,
        grounding_requirement=False, grounding_mode="balanced",
        low_confidence_action="clarify", no_retrieval_action="clarify",
        custom_instructions=None,
    )


def _profile():
    return BusinessProfile(
        tenant_id=uuid.uuid4(), summary="Lagos fintech", industry="fintech",
        offerings=["wallet", "bills"], audience=[], positioning="",
        key_entities=["LuranPay"], tone={}, source=BusinessProfileSource.AUDITOR,
    )


def test_grounding_block_present_when_profile_supplied(db):
    svc = RAGService(db)
    out = svc.assemble_prompt(
        ai_config=_ai_config(), user_query="hi", retrieved_chunks=[], conversation_history=[],
        company_name="LuranPay", business_profile=_profile(),
    )
    assert "About this business" in out.full_prompt
    assert "wallet" in out.full_prompt
    # escalation/grounding lines unchanged
    assert "No-retrieval action: clarify" in out.full_prompt


def test_grounding_is_noop_when_profile_absent(db):
    svc = RAGService(db)
    out = svc.assemble_prompt(
        ai_config=_ai_config(), user_query="hi", retrieved_chunks=[], conversation_history=[],
        company_name="LuranPay",
    )
    assert "About this business" not in out.full_prompt
    assert "No-retrieval action: clarify" in out.full_prompt  # baseline behavior intact


def test_grounding_block_token_budget(db):
    """Business profile block must not exceed ~150 tokens."""
    from app.utils.token_counter import count_tokens

    svc = RAGService(db)
    profile = BusinessProfile(
        tenant_id=uuid.uuid4(),
        summary="A " + "very long summary " * 40,  # intentionally verbose
        industry="fintech",
        offerings=["wallet", "bills", "loans", "insurance", "forex", "crypto", "savings"],
        audience=[],
        positioning="",
        key_entities=["LuranPay", "LuranWallet", "LuranBills"],
        tone={},
        source=BusinessProfileSource.AUDITOR,
    )
    block = svc._build_business_block(profile, token_budget=150)
    assert count_tokens(block) <= 150


def test_grounding_block_snapshot_no_profile(db):
    """With no profile, assemble_prompt output must be byte-for-byte identical to baseline."""
    svc = RAGService(db)
    cfg = _ai_config()

    # Baseline: call without business_profile
    baseline = svc.assemble_prompt(
        ai_config=cfg, user_query="test", retrieved_chunks=[], conversation_history=[],
        company_name="Acme",
    )
    # With explicit None: must match baseline exactly
    with_none = svc.assemble_prompt(
        ai_config=cfg, user_query="test", retrieved_chunks=[], conversation_history=[],
        company_name="Acme", business_profile=None,
    )
    assert baseline.full_prompt == with_none.full_prompt
