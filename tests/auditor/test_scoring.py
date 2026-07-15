"""Exhaustive tests for app.auditor.scoring — pure deterministic scoring."""
from app.auditor import scoring


def _expected():
    return [
        {"item": "FAQ", "dimensions": ["knowledge"], "weight": 1.0},
        {"item": "Guide", "dimensions": ["knowledge"], "weight": 1.0},
        {"item": "Refund policy", "dimensions": ["support"], "weight": 1.0},
        {"item": "Pricing", "dimensions": ["conversion"], "weight": 1.0},
    ]


def test_full_coverage_scores_100():
    s = scoring.score(_expected(), {"faq", "guide", "refund policy", "pricing"})
    assert s == {"knowledge": 100, "support": 100, "conversion": 100, "overall": 100, "projected": 100}


def test_partial_coverage_dimension_math():
    # knowledge: 1 of 2 found -> 50; support 0 -> 0; conversion 1 of 1 -> 100
    s = scoring.score(_expected(), {"faq", "pricing"})
    assert s["knowledge"] == 50
    assert s["support"] == 0
    assert s["conversion"] == 100
    # overall = 50*0.40 + 0*0.35 + 100*0.25 = 20 + 0 + 25 = 45
    assert s["overall"] == 45
    assert s["projected"] == 100  # all gaps added


def test_empty_expected_map_is_zero_not_crash():
    s = scoring.score([], set())
    assert s == {"knowledge": 0, "support": 0, "conversion": 0, "overall": 0, "projected": 0}


def test_determinism_same_inputs_same_output():
    a = scoring.score(_expected(), {"faq"})
    b = scoring.score(_expected(), {"faq"})
    assert a == b


def test_none_found_scores_zero():
    s = scoring.score(_expected(), set())
    assert s["knowledge"] == 0
    assert s["support"] == 0
    assert s["conversion"] == 0
    assert s["overall"] == 0
    assert s["projected"] == 100


def test_case_insensitive_found_items():
    # found_items should be normalised to lowercase before matching
    s = scoring.score(_expected(), {"FAQ", "GUIDE", "REFUND POLICY", "PRICING"})
    assert s == {"knowledge": 100, "support": 100, "conversion": 100, "overall": 100, "projected": 100}


def test_projected_gte_overall():
    s = scoring.score(_expected(), {"faq"})
    assert s["projected"] >= s["overall"]


def test_dimension_with_zero_weight_no_crash():
    # All items in "support" have weight 0 — must not divide by zero
    expected = [
        {"item": "FAQ", "dimensions": ["knowledge"], "weight": 1.0},
        {"item": "Zero-weight support", "dimensions": ["support"], "weight": 0.0},
    ]
    s = scoring.score(expected, {"faq", "zero-weight support"})
    assert s["knowledge"] == 100
    assert s["support"] == 0  # zero-weight dimension → 0
    assert s["overall"] == round(100 * 0.40 + 0 * 0.35 + 0 * 0.25)


def test_multi_dimension_item_counted_in_each():
    # An item tagged with both knowledge and conversion
    expected = [
        {"item": "Case studies", "dimensions": ["knowledge", "conversion"], "weight": 1.0},
        {"item": "FAQ", "dimensions": ["knowledge"], "weight": 1.0},
    ]
    s = scoring.score(expected, {"case studies"})
    # knowledge: 1/2 found -> 50
    assert s["knowledge"] == 50
    # conversion: 1/1 found -> 100
    assert s["conversion"] == 100
    # support: 0 items -> 0
    assert s["support"] == 0


def test_overall_weighted_blend_exact():
    # Manually verify 0.40/0.35/0.25 blend
    # knowledge=80, support=60, conversion=40
    # overall = round(80*0.40 + 60*0.35 + 40*0.25) = round(32+21+10) = round(63) = 63
    expected = [
        {"item": "A", "dimensions": ["knowledge"], "weight": 4.0},
        {"item": "B", "dimensions": ["knowledge"], "weight": 1.0},
        {"item": "C", "dimensions": ["support"], "weight": 3.0},
        {"item": "D", "dimensions": ["support"], "weight": 2.0},
        {"item": "E", "dimensions": ["conversion"], "weight": 2.0},
        {"item": "F", "dimensions": ["conversion"], "weight": 3.0},
    ]
    # Found: A(k,4), C(s,3), E(c,2)
    # knowledge=4/5=80, support=3/5=60, conversion=2/5=40
    s = scoring.score(expected, {"a", "c", "e"})
    assert s["knowledge"] == 80
    assert s["support"] == 60
    assert s["conversion"] == 40
    assert s["overall"] == 63


def test_return_dict_has_all_required_keys():
    s = scoring.score(_expected(), {"faq"})
    assert set(s.keys()) == {"knowledge", "support", "conversion", "overall", "projected"}


def test_all_scores_are_ints():
    s = scoring.score(_expected(), {"faq", "guide"})
    for key, val in s.items():
        assert isinstance(val, int), f"{key} is not int: {type(val)}"
