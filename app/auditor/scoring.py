"""Pure deterministic AI-readiness scoring. No LLM, no network."""

from __future__ import annotations

_DIMENSIONS = ("knowledge", "support", "conversion")
_OVERALL_WEIGHTS = {"knowledge": 0.40, "support": 0.35, "conversion": 0.25}


def _dimension_score(expected_map: list[dict], found_items: set[str], dimension: str) -> int:
    total_weight = 0.0
    found_weight = 0.0
    for item in expected_map:
        if dimension not in (item.get("dimensions") or []):
            continue
        weight = float(item.get("weight") or 0.0)
        total_weight += weight
        if str(item.get("item", "")).lower() in found_items:
            found_weight += weight
    if total_weight == 0.0:
        return 0
    return round(found_weight / total_weight * 100)


def score(expected_map: list[dict], found_items: set[str]) -> dict:
    """Compute AI-readiness scores from an expected-content map and found items.

    Args:
        expected_map: list of dicts with keys ``item``, ``dimensions``, ``weight``.
                      Each item comes from ``gaps.build_expected_map``.
        found_items: set of lowercased item labels currently satisfied on the site.

    Returns:
        dict with int scores: knowledge, support, conversion, overall, projected.
        ``projected`` is the score the site would reach after all gap items are added.
    """
    found_items = {f.lower() for f in found_items}
    dims = {d: _dimension_score(expected_map, found_items, d) for d in _DIMENSIONS}
    overall = round(sum(dims[d] * _OVERALL_WEIGHTS[d] for d in _DIMENSIONS))

    all_items = {str(i.get("item", "")).lower() for i in expected_map}
    proj_dims = {d: _dimension_score(expected_map, all_items, d) for d in _DIMENSIONS}
    projected = round(sum(proj_dims[d] * _OVERALL_WEIGHTS[d] for d in _DIMENSIONS))

    return {
        "knowledge": dims["knowledge"],
        "support": dims["support"],
        "conversion": dims["conversion"],
        "overall": overall,
        "projected": projected,
    }
