import json
from app.sales.intent import SALES_RESPONSE_SCHEMA, parse_sales_output


def test_schema_has_offer_quote_and_plan_interest():
    sig = SALES_RESPONSE_SCHEMA["properties"]["signals"]
    cap = SALES_RESPONSE_SCHEMA["properties"]["captured"]
    assert "offer_quote" in sig["properties"] and "offer_quote" in sig["required"]
    assert "plan_interest" in cap["properties"] and "plan_interest" in cap["required"]
    assert sig["additionalProperties"] is False and cap["additionalProperties"] is False


def test_parse_passes_offer_quote_and_plan_interest():
    raw = json.dumps({
        "reply": "Sure!", "intent": "PRICING_INQUIRY",
        "signals": {"asked_features": False, "requested_demo": False, "mentioned_competitor": False,
                    "ready_to_buy": True, "offer_meeting": False, "offer_quote": True},
        "captured": {"name": None, "email": None, "company": None, "team_size": "50",
                     "industry": None, "plan_interest": "Growth"},
    })
    out = parse_sales_output(raw)
    assert out["signals"]["offer_quote"] is True
    assert out["captured"]["plan_interest"] == "Growth"
