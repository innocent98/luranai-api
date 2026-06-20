"""Expected-content map (LLM) + pure gap analysis (expected - found, impact x demand)."""

from __future__ import annotations

from app.auditor.llm import complete_json

_SYSTEM = (
    "You define the content a business like this SHOULD publish to serve and convert customers. "
    "Return JSON {\"expected\": [...]} where each item has: item (string), dimensions (subset of "
    "[knowledge, support, conversion]), weight (0-1 float importance), kind (faq|policy|product|"
    "pricing|guide|contact|testimonial|other), why_it_matters (string), recommended_content (string), "
    "demand_count (int, 0 unless a question count is supplied). Adapt to THIS business."
)


async def build_expected_map(
    profile: dict,
    *,
    unanswered_questions: list[str] | None = None,
) -> list[dict]:
    """Call the LLM to build an expected-content map for this business profile.

    Each item returned: {item, dimensions, weight, kind, why_it_matters,
    recommended_content, demand_count}.

    unanswered_questions: optional list of customer questions that have not
    been answered by the website (Stage 5 input).  At onboarding this is
    empty, so demand_count will be 0 for all items.
    """
    questions = unanswered_questions or []
    user = f"BUSINESS PROFILE: {profile}\n\nUNANSWERED CUSTOMER QUESTIONS: {questions}"
    data = await complete_json("expected_map", _SYSTEM, user, temperature=0.1)
    return data.get("expected") or []


def compute_gaps(
    expected_map: list[dict],
    found_kinds: set[str],
    found_categories: set[str],
) -> list[dict]:
    """Pure function: gap = expected − found; rank by impact × demand_count.

    An expected item is considered FOUND (not a gap) when its ``kind`` is in
    ``found_kinds`` OR its ``item`` label (case-insensitive) is in
    ``found_categories``.

    Each gap dict: {title, why_it_matters, recommended_content, kind,
    demand_count, impact}.

    Ranking: ``impact × max(demand_count, 1)`` so that at onboarding
    (demand_count=0) items are ordered by impact alone.
    """
    found_categories_lower = {c.lower() for c in found_categories}
    gaps_out: list[dict] = []

    for item in expected_map:
        kind = str(item.get("kind") or "other")
        label = str(item.get("item") or "").lower()

        if kind in found_kinds or label in found_categories_lower:
            continue

        weight = float(item.get("weight") or 0.0)
        demand = int(item.get("demand_count") or 0)

        gaps_out.append(
            {
                "title": item.get("item"),
                "why_it_matters": item.get("why_it_matters"),
                "recommended_content": item.get("recommended_content"),
                "kind": kind,
                "demand_count": demand,
                "impact": weight,
            }
        )

    gaps_out.sort(
        key=lambda g: g["impact"] * max(g["demand_count"], 1),
        reverse=True,
    )
    return gaps_out
