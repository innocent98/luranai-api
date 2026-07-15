"""Tests for app/auditor/classify.py — adaptive per-page classification."""
from unittest.mock import AsyncMock, patch

import pytest

from app.auditor import classify
from app.auditor.extraction import ExtractedPage
from app.db.models.audit import AuditEntryKind
from app.db.models.knowledge import SourceType


def test_kind_to_source_type_map_is_exhaustive():
    for kind in AuditEntryKind:
        assert kind in classify.KIND_TO_SOURCE_TYPE
    assert classify.map_kind(AuditEntryKind.FAQ) == SourceType.FAQ
    assert classify.map_kind(AuditEntryKind.GUIDE) == SourceType.HELP_DOC
    assert classify.map_kind(AuditEntryKind.POLICY) == SourceType.POLICY
    for k in (AuditEntryKind.PRODUCT, AuditEntryKind.PRICING, AuditEntryKind.CONTACT, AuditEntryKind.TESTIMONIAL):
        assert classify.map_kind(k) == SourceType.OTHER
    assert classify.map_kind(AuditEntryKind.OTHER) == SourceType.OTHER


@pytest.mark.asyncio
async def test_classify_page_parses_llm_output():
    fake = {"category": "onboarding faq", "kind": "faq", "importance_rank": 2, "keep": True}
    page = ExtractedPage(url="https://x.com/faq", title="FAQ", text="...", content_hash="h")
    with patch("app.auditor.classify.complete_json", AsyncMock(return_value=fake)):
        c = await classify.classify_page(page, {"industry": "fintech"})
    assert c.kind == AuditEntryKind.FAQ
    assert c.category == "onboarding faq"
    assert c.keep is True


@pytest.mark.asyncio
async def test_classify_pages_filters_skip():
    keep = {"category": "c", "kind": "product", "importance_rank": 1, "keep": True}
    skip = {"category": "c", "kind": "other", "importance_rank": 9, "keep": False}
    pages = [
        ExtractedPage(url="https://x.com/a", title="A", text="t", content_hash="h1"),
        ExtractedPage(url="https://x.com/b", title="B", text="t", content_hash="h2"),
    ]
    with patch("app.auditor.classify.complete_json", AsyncMock(side_effect=[keep, skip])):
        out = await classify.classify_pages(pages, {})
    assert len(out) == 1
    assert out[0][0].url == "https://x.com/a"


@pytest.mark.asyncio
async def test_classify_page_adaptive_free_form_category():
    """The category is preserved verbatim from LLM output — free-form, business-specific."""
    fake = {
        "category": "API & integrations",
        "kind": "guide",
        "importance_rank": 1,
        "keep": True,
    }
    page = ExtractedPage(url="https://x.com/docs/api", title="API Docs", text="...", content_hash="h2")
    with patch("app.auditor.classify.complete_json", AsyncMock(return_value=fake)):
        c = await classify.classify_page(page, {"industry": "saas"})
    assert c.category == "API & integrations"
    assert c.kind == AuditEntryKind.GUIDE
    assert c.importance_rank == 1


@pytest.mark.asyncio
async def test_classify_page_unknown_kind_coerces_to_other():
    """Unknown kind values from LLM are safely coerced to OTHER."""
    fake = {"category": "misc", "kind": "bogus_kind", "importance_rank": 5, "keep": True}
    page = ExtractedPage(url="https://x.com/misc", title="Misc", text="...", content_hash="h3")
    with patch("app.auditor.classify.complete_json", AsyncMock(return_value=fake)):
        c = await classify.classify_page(page, {})
    assert c.kind == AuditEntryKind.OTHER
    assert classify.map_kind(c.kind) == SourceType.OTHER


@pytest.mark.asyncio
async def test_classify_page_sparse_llm_keys_safe_defaults():
    """Missing keys in LLM output should not raise KeyError — safe defaults applied."""
    fake: dict = {}  # completely empty response
    page = ExtractedPage(url="https://x.com/empty", title=None, text="...", content_hash="h4")
    with patch("app.auditor.classify.complete_json", AsyncMock(return_value=fake)):
        c = await classify.classify_page(page, {})
    assert c.category == "uncategorized"
    assert c.kind == AuditEntryKind.OTHER
    assert c.importance_rank == 99
    assert c.keep is True  # default to keep so nothing is silently dropped


@pytest.mark.asyncio
async def test_classify_pages_all_kept_when_all_keep_true():
    """classify_pages returns all pages when LLM marks all keep=True."""
    responses = [
        {"category": "pricing", "kind": "pricing", "importance_rank": 1, "keep": True},
        {"category": "returns policy", "kind": "policy", "importance_rank": 2, "keep": True},
    ]
    pages = [
        ExtractedPage(url="https://x.com/pricing", title="Pricing", text="t", content_hash="h5"),
        ExtractedPage(url="https://x.com/returns", title="Returns", text="t", content_hash="h6"),
    ]
    with patch("app.auditor.classify.complete_json", AsyncMock(side_effect=responses)):
        out = await classify.classify_pages(pages, {"industry": "ecommerce"})
    assert len(out) == 2
    assert out[0][1].kind == AuditEntryKind.PRICING
    assert out[1][1].kind == AuditEntryKind.POLICY


@pytest.mark.asyncio
async def test_classify_pages_empty_input():
    """classify_pages with no pages returns empty list without calling LLM."""
    with patch("app.auditor.classify.complete_json", AsyncMock()) as mock_llm:
        out = await classify.classify_pages([], {})
    assert out == []
    mock_llm.assert_not_called()
