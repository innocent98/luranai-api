"""Tests for app/auditor/gaps.py — expected-content map + gap analysis."""

from unittest.mock import AsyncMock, patch

import pytest

from app.auditor import gaps


# ---------------------------------------------------------------------------
# compute_gaps — pure function tests
# ---------------------------------------------------------------------------


def test_compute_gaps_is_expected_minus_found_ranked():
    expected = [
        {
            "item": "Refund policy",
            "dimensions": ["support"],
            "weight": 0.3,
            "kind": "policy",
            "why_it_matters": "trust",
            "recommended_content": "Add a refund policy page",
            "demand_count": 4,
        },
        {
            "item": "Pricing page",
            "dimensions": ["conversion"],
            "weight": 0.5,
            "kind": "pricing",
            "why_it_matters": "convert",
            "recommended_content": "Publish pricing",
            "demand_count": 2,
        },
        {
            "item": "FAQ",
            "dimensions": ["knowledge"],
            "weight": 0.2,
            "kind": "faq",
            "why_it_matters": "deflect",
            "recommended_content": "Add FAQ",
            "demand_count": 1,
        },
    ]
    found_kinds = {"faq"}  # FAQ already present -> not a gap
    result = gaps.compute_gaps(expected, found_kinds, set())
    titles = [g["title"] for g in result]
    assert "FAQ" not in titles
    # ranked by impact(weight) x demand_count: refund(0.3*4=1.2) > pricing(0.5*2=1.0)
    assert titles == ["Refund policy", "Pricing page"]


def test_compute_gaps_empty_when_all_found():
    expected = [
        {
            "item": "FAQ",
            "dimensions": ["knowledge"],
            "weight": 0.2,
            "kind": "faq",
            "why_it_matters": "x",
            "recommended_content": "y",
            "demand_count": 1,
        }
    ]
    assert gaps.compute_gaps(expected, {"faq"}, set()) == []


def test_compute_gaps_found_via_category_label():
    """An item matched by found_categories (case-insensitive) is not a gap."""
    expected = [
        {
            "item": "Contact Us",
            "dimensions": ["support"],
            "weight": 0.4,
            "kind": "contact",
            "why_it_matters": "reach",
            "recommended_content": "Add contact form",
            "demand_count": 0,
        }
    ]
    # kind not in found_kinds but label in found_categories
    result = gaps.compute_gaps(expected, set(), {"contact us"})
    assert result == []


def test_compute_gaps_demand_zero_orders_by_impact():
    """At onboarding demand_count=0 — items rank by impact alone."""
    expected = [
        {
            "item": "Guide",
            "dimensions": ["knowledge"],
            "weight": 0.3,
            "kind": "guide",
            "why_it_matters": "educate",
            "recommended_content": "Write a guide",
            "demand_count": 0,
        },
        {
            "item": "Testimonials",
            "dimensions": ["conversion"],
            "weight": 0.7,
            "kind": "testimonial",
            "why_it_matters": "trust",
            "recommended_content": "Add testimonials",
            "demand_count": 0,
        },
    ]
    result = gaps.compute_gaps(expected, set(), set())
    titles = [g["title"] for g in result]
    # Higher weight (0.7) should rank first
    assert titles == ["Testimonials", "Guide"]


def test_compute_gaps_gap_shape():
    """Each gap dict contains the required keys."""
    expected = [
        {
            "item": "Pricing page",
            "dimensions": ["conversion"],
            "weight": 0.5,
            "kind": "pricing",
            "why_it_matters": "convert",
            "recommended_content": "Publish pricing",
            "demand_count": 3,
        }
    ]
    result = gaps.compute_gaps(expected, set(), set())
    assert len(result) == 1
    gap = result[0]
    assert set(gap.keys()) >= {"title", "why_it_matters", "recommended_content", "kind", "demand_count", "impact"}
    assert gap["title"] == "Pricing page"
    assert gap["impact"] == 0.5
    assert gap["demand_count"] == 3
    assert gap["kind"] == "pricing"


def test_compute_gaps_safe_defaults_missing_fields():
    """Items with missing optional fields don't raise."""
    expected = [{"item": "Something", "kind": "other"}]
    result = gaps.compute_gaps(expected, set(), set())
    assert len(result) == 1
    gap = result[0]
    assert gap["impact"] == 0.0
    assert gap["demand_count"] == 0


# ---------------------------------------------------------------------------
# build_expected_map — async / LLM tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_expected_map_passes_unanswered_questions():
    fake = {
        "expected": [
            {
                "item": "Pricing",
                "dimensions": ["conversion"],
                "weight": 0.4,
                "kind": "pricing",
                "demand_count": 0,
            }
        ]
    }
    with patch("app.auditor.gaps.complete_json", AsyncMock(return_value=fake)) as m:
        out = await gaps.build_expected_map(
            {"industry": "saas"}, unanswered_questions=["do you offer trials?"]
        )
    assert out[0]["item"] == "Pricing"
    assert "do you offer trials?" in m.call_args.args[2]  # user prompt carries questions


@pytest.mark.asyncio
async def test_build_expected_map_empty_questions_by_default():
    """No unanswered_questions → empty list in prompt, returns expected list."""
    fake = {
        "expected": [
            {
                "item": "FAQ",
                "dimensions": ["knowledge"],
                "weight": 0.2,
                "kind": "faq",
                "demand_count": 0,
            }
        ]
    }
    with patch("app.auditor.gaps.complete_json", AsyncMock(return_value=fake)) as m:
        out = await gaps.build_expected_map({"industry": "retail"})
    assert out[0]["item"] == "FAQ"
    # The user prompt should contain an empty list representation
    assert "[]" in m.call_args.args[2]


@pytest.mark.asyncio
async def test_build_expected_map_safe_default_on_empty_llm_response():
    """If LLM returns empty dict, return empty list (safe default)."""
    with patch("app.auditor.gaps.complete_json", AsyncMock(return_value={})):
        out = await gaps.build_expected_map({"industry": "saas"})
    assert out == []


@pytest.mark.asyncio
async def test_build_expected_map_uses_low_temperature():
    """build_expected_map calls complete_json with temperature=0.1."""
    fake = {"expected": []}
    with patch("app.auditor.gaps.complete_json", AsyncMock(return_value=fake)) as m:
        await gaps.build_expected_map({"industry": "saas"})
    _, kwargs = m.call_args
    assert kwargs.get("temperature") == 0.1


@pytest.mark.asyncio
async def test_build_expected_map_uses_expected_map_task():
    """build_expected_map routes to the 'expected_map' task."""
    fake = {"expected": []}
    with patch("app.auditor.gaps.complete_json", AsyncMock(return_value=fake)) as m:
        await gaps.build_expected_map({"industry": "saas"})
    task_arg = m.call_args.args[0]
    assert task_arg == "expected_map"


@pytest.mark.asyncio
async def test_build_expected_map_retries_on_empty_then_succeeds():
    """An empty (malformed) LLM response is retried; a later non-empty result wins."""
    calls = []

    async def flaky(task, system, user, *, temperature=0.2):
        calls.append(task)
        if len(calls) < 2:
            return {}  # transient empty
        return {"expected": [{"item": "Pricing", "kind": "pricing", "weight": 1.0}]}

    with patch("app.auditor.gaps.complete_json", flaky):
        out = await gaps.build_expected_map({"industry": "retail"})
    assert len(out) == 1
    assert len(calls) == 2  # retried once before succeeding
