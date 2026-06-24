import types
from app.sales.scoring import score_lead, tier_for, SIGNALS

def _lead(**kw):
    base = dict(email=None, company=None, team_size=None, industry=None, utm=None)
    base.update(kw)
    return types.SimpleNamespace(**base)

def test_empty_lead_scores_zero_and_lists_all_signals():
    r = score_lead(_lead())
    assert r["score"] == 0
    assert r["tier"] == "cold"
    assert len(r["breakdown"]) == len(SIGNALS) == 9
    assert all(item["fired"] is False for item in r["breakdown"])

def test_field_signals_fire_from_lead_columns():
    r = score_lead(_lead(email="a@b.com", company="Acme", team_size=120,
                         industry="Bank", utm={"medium": "cpc"}))
    fired = {i["key"] for i in r["breakdown"] if i["fired"]}
    assert {"provided_email", "company_name", "team_over_50", "enterprise", "paid_ad"} <= fired
    # 20 + 10 + 15 + 20 + 5 = 70
    assert r["score"] == 70
    assert r["tier"] == "hot"

def test_conversation_signals_come_from_signals_arg():
    r = score_lead(_lead(email="a@b.com"),
                   signals={"requested_demo": True, "ready_to_buy": True})
    fired = {i["key"] for i in r["breakdown"] if i["fired"]}
    assert {"provided_email", "requested_demo", "ready_to_buy"} == fired
    assert r["score"] == 20 + 15 + 25  # 60 → warm

def test_score_caps_at_100():
    r = score_lead(
        _lead(email="a@b.com", company="Acme", team_size=120, industry="Bank",
              utm={"medium": "cpc"}),
        signals={"asked_features": True, "requested_demo": True,
                 "mentioned_competitor": True, "ready_to_buy": True},
    )  # 70 + 10 + 15 + 10 + 25 = 130
    assert r["score"] == 100
    assert r["tier"] == "sales_ready"

def test_tier_boundaries():
    assert tier_for(30) == "cold"
    assert tier_for(31) == "warm"
    assert tier_for(60) == "warm"
    assert tier_for(61) == "hot"
    assert tier_for(85) == "hot"
    assert tier_for(86) == "sales_ready"
