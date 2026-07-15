"""Tests for the offer_meeting signal in SALES_RESPONSE_SCHEMA and parse_sales_output."""
import json

from app.sales.intent import SALES_RESPONSE_SCHEMA, parse_sales_output


def test_schema_includes_offer_meeting():
    sig = SALES_RESPONSE_SCHEMA["properties"]["signals"]
    assert "offer_meeting" in sig["properties"]
    assert "offer_meeting" in sig["required"]
    assert sig["additionalProperties"] is False


def test_parse_passes_offer_meeting_through():
    raw = json.dumps({
        "reply": "Sure!", "intent": "DEMO_REQUEST",
        "signals": {"asked_features": False, "requested_demo": True,
                    "mentioned_competitor": False, "ready_to_buy": False, "offer_meeting": True},
        "captured": {"name": None, "email": None, "company": None, "team_size": None, "industry": None},
    })
    out = parse_sales_output(raw)
    assert out["signals"]["offer_meeting"] is True
