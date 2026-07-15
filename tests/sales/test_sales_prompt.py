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
    """Issue 3a: even when scheduling IS enabled, a pricing chat or contact
    collection must NOT trigger a meeting — only an explicit meeting request."""
    d = build_sales_directive(["Q?"], meeting_booking_enabled=True).lower()
    assert "offer_meeting" in d
    assert "pricing" in d and "contact collection" in d


def test_directive_includes_known_facts_and_no_reask():
    """State-threading: facts already captured must be pinned so the agent
    stops re-asking (the 'How large is your team?' twice bug)."""
    d = build_sales_directive(
        ["How large is your team?"],
        known_facts={"team_size": "10-15", "plan_interest": "Basic"},
    )
    assert "10-15" in d and "Basic" in d
    low = d.lower()
    assert "do not ask" in low or "don't ask" in low or "already" in low


def test_directive_no_known_facts_block_when_empty():
    marker = "already provided this conversation"
    assert marker not in build_sales_directive(["Q?"], known_facts={})
    assert marker not in build_sales_directive(["Q?"], known_facts=None)
    # An all-empty dict must also render no block.
    assert marker not in build_sales_directive(["Q?"], known_facts={"team_size": None, "name": ""})


def test_directive_recommends_higher_tier_on_overlap():
    """Tenant policy: when a team size fits multiple plans, recommend the higher one."""
    d = build_sales_directive(["Q?"]).lower()
    assert "higher" in d and "plan" in d


def test_directive_offers_scheduling_when_meeting_enabled():
    """When meeting booking is enabled, the agent must know it CAN schedule and
    offer it — not deflect to 'contact support' (the observed bug)."""
    d = build_sales_directive(["Q?"], meeting_booking_enabled=True).lower()
    assert "offer_meeting" in d
    assert "available times" in d
    assert "schedule" in d  # it can schedule right here


def test_directive_no_scheduling_claim_when_meeting_disabled():
    d = build_sales_directive(["Q?"], meeting_booking_enabled=False).lower()
    assert "not available" in d or "cannot schedule" in d
    assert "connect" in d  # offer team handoff instead


def test_directive_includes_company_website():
    d = build_sales_directive(["Q?"], company_website="https://weysure.example.com")
    assert "https://weysure.example.com" in d


def test_directive_forbids_placeholder_output():
    """No more '[insert URL here]' — the model must not fabricate placeholders."""
    d = build_sales_directive(["Q?"]).lower()
    assert "placeholder" in d or "[insert" in d


def test_parse_valid_and_invalid():
    good = '{"reply":"Hi","intent":"PRICING_INQUIRY","signals":{"asked_features":false,"requested_demo":false,"mentioned_competitor":false,"ready_to_buy":false,"offer_meeting":false,"offer_quote":false},"captured":{"name":null,"email":null,"company":null,"team_size":null,"industry":null,"plan_interest":null}}'
    out = parse_sales_output(good)
    assert out["intent"] == "PRICING_INQUIRY" and out["reply"] == "Hi"
    assert parse_sales_output("not json") is None
    assert parse_sales_output('{"reply":"x"}') is None  # missing keys
