from app.sales.intent import SALES_RESPONSE_SCHEMA, build_sales_directive, parse_sales_output


def test_schema_has_required_keys():
    props = SALES_RESPONSE_SCHEMA["properties"]
    assert set(props) == {"reply", "intent", "signals", "captured"}
    assert set(props["signals"]["properties"]) == {
        "asked_features", "requested_demo", "mentioned_competitor", "ready_to_buy",
        "offer_meeting"}
    assert set(props["captured"]["properties"]) == {
        "name", "email", "company", "team_size", "industry"}


def test_directive_mentions_one_question_and_no_forms():
    d = build_sales_directive([{"question": "How large is your team?"}])
    assert "one" in d.lower() and "no forms" in d.lower()
    assert "How large is your team?" in d


def test_directive_accepts_plain_string_question():
    d = build_sales_directive(["How big is your team?"])
    assert "How big is your team?" in d


def test_parse_valid_and_invalid():
    good = '{"reply":"Hi","intent":"PRICING_INQUIRY","signals":{"asked_features":false,"requested_demo":false,"mentioned_competitor":false,"ready_to_buy":false},"captured":{"name":null,"email":null,"company":null,"team_size":null,"industry":null}}'
    out = parse_sales_output(good)
    assert out["intent"] == "PRICING_INQUIRY" and out["reply"] == "Hi"
    assert parse_sales_output("not json") is None
    assert parse_sales_output('{"reply":"x"}') is None  # missing keys
