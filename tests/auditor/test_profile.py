"""Tests for BusinessProfile synthesis, persistence, and reuse-unless-changed."""

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.auditor import profile
from app.auditor.extraction import ExtractedPage
from app.db.models.business_profile import BusinessProfile


def _pages():
    return [
        ExtractedPage(url="https://x.com/", title="Home", text="We are a Lagos fintech.", content_hash="h1"),
        ExtractedPage(url="https://x.com/about", title="About", text="Wallets and bills.", content_hash="h2"),
    ]


@pytest.mark.asyncio
async def test_synthesize_profile_returns_structured_dict():
    fake = {
        "summary": "Lagos fintech", "industry": "fintech",
        "offerings": ["wallet"], "audience": ["SMEs"], "positioning": "fast",
        "key_entities": ["LuranPay"], "tone": {"style": "warm"}, "confidence": 0.8,
    }
    with patch("app.auditor.profile.complete_json", AsyncMock(return_value=fake)):
        out = await profile.synthesize_profile(_pages())
    assert out["industry"] == "fintech"
    assert out["offerings"] == ["wallet"]


def test_persist_profile_upserts_one_per_tenant(db):
    tid = uuid.uuid4()
    data = {"summary": "s", "industry": "i", "offerings": [], "audience": [],
            "positioning": "p", "key_entities": [], "tone": {}, "confidence": 0.5}
    bp1 = profile.persist_profile(db, tid, data, model_version="gpt-4o")
    db.flush()
    data["industry"] = "updated"
    bp2 = profile.persist_profile(db, tid, data, model_version="gpt-4o")
    db.flush()
    assert bp1.id == bp2.id  # upsert, not duplicate
    assert db.query(BusinessProfile).filter(BusinessProfile.tenant_id == tid).count() == 1
    assert bp2.industry == "updated"


def test_content_signature_stable_regardless_of_order():
    a = profile.content_signature(_pages())
    b = profile.content_signature(list(reversed(_pages())))
    assert a == b


def test_synthesize_profile_normalises_missing_llm_fields():
    """LLM returning sparse dict should still yield a fully-keyed dict with safe defaults."""
    sparse = {"industry": "saas"}

    async def run():
        with patch("app.auditor.profile.complete_json", AsyncMock(return_value=sparse)):
            return await profile.synthesize_profile(_pages())

    import asyncio
    out = asyncio.get_event_loop().run_until_complete(run())
    assert out["offerings"] == []
    assert out["tone"] == {}
    assert out["industry"] == "saas"
    assert out["summary"] is None


def test_persist_profile_bumps_refreshed_at(db):
    """refreshed_at should be set on every persist call."""
    tid = uuid.uuid4()
    data = {"summary": "s", "industry": "i", "offerings": [], "audience": [],
            "positioning": "p", "key_entities": [], "tone": {}, "confidence": 0.9}
    bp = profile.persist_profile(db, tid, data, model_version="gpt-4o")
    db.flush()
    assert bp.refreshed_at is not None


@pytest.mark.asyncio
async def test_synthesize_or_reuse_skips_llm_when_signature_unchanged(db):
    """When prior_signature matches current signature, the LLM is NOT called."""
    tid = uuid.uuid4()
    pages = _pages()
    sig = profile.content_signature(pages)

    # Pre-seed an existing profile for this tenant
    data = {"summary": "existing", "industry": "fintech", "offerings": [], "audience": [],
            "positioning": "p", "key_entities": [], "tone": {}, "confidence": 0.7}
    existing_bp = profile.persist_profile(db, tid, data, model_version="gpt-4o")
    db.flush()

    fake_llm = AsyncMock(return_value={})
    with patch("app.auditor.profile.complete_json", fake_llm):
        bp, returned_sig = await profile.synthesize_or_reuse(db, tid, pages, prior_signature=sig)

    fake_llm.assert_not_called()
    assert bp.id == existing_bp.id
    assert returned_sig == sig


@pytest.mark.asyncio
async def test_synthesize_or_reuse_re_synthesises_when_signature_changes(db):
    """When prior_signature differs, a new synthesis is triggered and the row is updated."""
    tid = uuid.uuid4()
    pages = _pages()

    fake_result = {
        "summary": "new synthesis", "industry": "logistics",
        "offerings": ["tracking"], "audience": ["B2B"], "positioning": "fast",
        "key_entities": ["LuranShip"], "tone": {"style": "bold"}, "confidence": 0.9,
    }
    fake_llm = AsyncMock(return_value=fake_result)
    with patch("app.auditor.profile.complete_json", fake_llm):
        bp, sig = await profile.synthesize_or_reuse(
            db, tid, pages, prior_signature="stale_sig_that_differs"
        )

    fake_llm.assert_called_once()
    assert bp.industry == "logistics"
    assert sig == profile.content_signature(pages)


@pytest.mark.asyncio
async def test_synthesize_or_reuse_creates_profile_when_none_exists(db):
    """When no profile exists yet, it synthesizes and persists a new one."""
    tid = uuid.uuid4()
    pages = _pages()

    fake_result = {
        "summary": "brand new", "industry": "edtech",
        "offerings": ["courses"], "audience": ["students"], "positioning": "accessible",
        "key_entities": ["LuranLearn"], "tone": {"style": "friendly"}, "confidence": 0.85,
    }
    with patch("app.auditor.profile.complete_json", AsyncMock(return_value=fake_result)):
        bp, sig = await profile.synthesize_or_reuse(db, tid, pages, prior_signature=None)

    db.flush()
    assert db.query(BusinessProfile).filter(BusinessProfile.tenant_id == tid).count() == 1
    assert bp.industry == "edtech"
    assert sig == profile.content_signature(pages)
