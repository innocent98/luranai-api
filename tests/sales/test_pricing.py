from app.sales.pricing import compute_quote

TIERS = [
    {"name": "Starter", "base_price": 20000, "min_users": 1, "max_users": 10, "per_seat_overage_price": 2500},
    {"name": "Growth", "base_price": 45000, "min_users": 11, "max_users": 50, "per_seat_overage_price": 3500},
]


def test_base_only_no_overage_no_discount():
    q = compute_quote(TIERS, 0, "NGN", team_size=30, plan_interest="Growth")
    assert q["total"] == 45000
    assert q["line_items"] == [{"label": "Growth", "detail": "11–50 users", "amount": 45000}]


def test_overage_applied():
    q = compute_quote(TIERS, 0, "NGN", team_size=58, plan_interest="Growth")
    # base 45000 + (58-50)*3500 = 45000 + 28000 = 73000
    assert q["total"] == 73000
    labels = [li["label"] for li in q["line_items"]]
    assert "Extra seats (×8)" in labels


def test_discount_applied():
    q = compute_quote(TIERS, 15, "NGN", team_size=58, plan_interest="Growth")
    # subtotal 73000; discount 73000*15//100 = 10950; total 62050
    assert q["total"] == 62050
    disc = [li for li in q["line_items"] if li["amount"] < 0]
    assert disc and disc[0]["amount"] == -10950 and disc[0]["detail"] == "−15%"


def test_match_by_team_size_when_no_plan():
    q = compute_quote(TIERS, 0, "NGN", team_size=5, plan_interest=None)
    assert q["line_items"][0]["label"] == "Starter"


def test_empty_tiers_returns_none():
    assert compute_quote([], 0, "NGN", team_size=10, plan_interest="Growth") is None


def test_no_match_returns_none():
    # team_size out of all ranges and plan name unknown
    assert compute_quote(TIERS, 0, "NGN", team_size=9999, plan_interest="Enterprise") is None


def test_integer_math_no_float():
    q = compute_quote(TIERS, 15, "NGN", team_size=58, plan_interest="Growth")
    assert all(isinstance(li["amount"], int) for li in q["line_items"])
    assert isinstance(q["total"], int)
