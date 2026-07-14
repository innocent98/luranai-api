from app.sales.intent import SALES_RESPONSE_SCHEMA, build_sales_directive, parse_sales_output


def test_schema_has_required_keys():
    props = SALES_RESPONSE_SCHEMA["properties"]
    assert set(props) == {"reply", "intent", "signals", "captured"}
    assert set(props["signals"]["properties"]) == {
        "asked_features",
        "requested_demo",
        "mentioned_competitor",
        "ready_to_buy",
        "offer_meeting",
        "offer_quote",
    }
    assert set(props["captured"]["properties"]) == {
        "name",
        "email",
        "company",
        "team_size",
        "industry",
        "plan_interest",
    }


def test_directive_mentions_one_question_and_no_forms():
    d = build_sales_directive([{"question": "How large is your team?"}])
    assert "one" in d.lower() and "no forms" in d.lower()
    assert "How large is your team?" in d


def test_directive_accepts_plain_string_question():
    d = build_sales_directive(["How big is your team?"])
    assert "How big is your team?" in d


_TIERS = [
    {
        "name": "Lite",
        "base_price": 50000,
        "min_users": 1,
        "max_users": 10,
        "per_seat_overage_price": 5000,
    },
    {
        "name": "Pro",
        "base_price": 150000,
        "min_users": 11,
        "max_users": 50,
        "per_seat_overage_price": 4000,
    },
]


def test_directive_lists_configured_plans():
    """The AI must be told the tenant's plans so it can answer plan questions
    instead of deflecting with 'I don't have plan details'."""
    d = build_sales_directive(["How large is your team?"], pricing_tiers=_TIERS, currency="NGN")
    assert "Lite" in d and "Pro" in d
    assert "50,000" in d and "150,000" in d
    assert "1" in d and "10" in d  # seat range surfaced


def test_directive_includes_annual_discount_when_set():
    d = build_sales_directive([], pricing_tiers=_TIERS, currency="NGN", annual_discount_pct=15)
    assert "15%" in d


def test_directive_no_plans_section_when_no_tiers():
    d = build_sales_directive(["Q?"], pricing_tiers=[], currency="NGN")
    assert "Available Plans" not in d


def test_directive_asks_one_question_per_reply():
    """Issue 2: avoid stacking a qualifying question + escalation offer in one reply."""
    d = build_sales_directive(["Q?"]).lower()
    assert "one question per reply" in d


def test_directive_escalation_offered_once():
    """Issue 2: don't tack 'connect you with our team' onto every reply."""
    d = build_sales_directive(["Q?"]).lower()
    assert "once" in d and "connect" in d


def test_directive_offer_meeting_not_for_pricing_or_escalation():
    """Issue 3a: a pricing chat or a name/email handoff must NOT trigger a meeting."""
    d = build_sales_directive(["Q?"]).lower()
    # meeting is only for an explicit demo/call, not contact-collection or pricing
    assert "offer_meeting" in d
    assert "collecting" in d or "contact info is not a meeting" in d


def test_parse_valid_and_invalid():
    good = '{"reply":"Hi","intent":"PRICING_INQUIRY","signals":{"asked_features":false,"requested_demo":false,"mentioned_competitor":false,"ready_to_buy":false,"offer_meeting":false,"offer_quote":false},"captured":{"name":null,"email":null,"company":null,"team_size":null,"industry":null,"plan_interest":null}}'
    out = parse_sales_output(good)
    assert out["intent"] == "PRICING_INQUIRY" and out["reply"] == "Hi"
    assert parse_sales_output("not json") is None
    assert parse_sales_output('{"reply":"x"}') is None  # missing keys
