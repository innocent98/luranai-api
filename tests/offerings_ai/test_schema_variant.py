"""T3 / plan risk 1: the strict catalog schema variant breaks no existing consumer."""

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.offerings.signals import parse_catalog_signals
from app.sales.intent import (
    SALES_RESPONSE_SCHEMA,
    SALES_RESPONSE_SCHEMA_OFFERINGS,
    parse_sales_output,
)
from app.sales.scoring import score_lead
from app.services.rag_service import RAGService

GOLDEN = json.loads((Path(__file__).parent / "golden_prompts.json").read_text())

_NEW = {"catalog_refs", "cart", "offered_price"}


def _walk_objects(node):
    if isinstance(node, dict):
        if node.get("type") == "object" or (
            isinstance(node.get("type"), list) and "object" in node["type"]
        ):
            yield node
        for v in node.values():
            yield from _walk_objects(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk_objects(v)


def test_existing_schema_is_untouched():
    assert SALES_RESPONSE_SCHEMA == GOLDEN["sales_format"]["json_schema"]["schema"]


def test_variant_is_a_superset_with_the_three_new_signals():
    base_sig = SALES_RESPONSE_SCHEMA["properties"]["signals"]["properties"]
    var_sig = SALES_RESPONSE_SCHEMA_OFFERINGS["properties"]["signals"]["properties"]
    assert set(var_sig) == set(base_sig) | _NEW
    assert set(SALES_RESPONSE_SCHEMA_OFFERINGS["properties"]) == set(
        SALES_RESPONSE_SCHEMA["properties"]
    )
    # the shared keys are identical, so the model answers them the same way
    for key in base_sig:
        assert var_sig[key] == base_sig[key]
    assert SALES_RESPONSE_SCHEMA["properties"]["signals"]["properties"].keys() & _NEW == set()


def test_variant_satisfies_openai_strict_rules():
    """Every object forbids extra keys and requires every property (strict mode)."""
    for obj in _walk_objects(SALES_RESPONSE_SCHEMA_OFFERINGS):
        assert obj["additionalProperties"] is False
        assert set(obj["required"]) == set(obj["properties"])


def test_variant_is_a_separate_object():
    snapshot = copy.deepcopy(SALES_RESPONSE_SCHEMA)
    SALES_RESPONSE_SCHEMA_OFFERINGS["properties"]["signals"]["properties"]["cart"]["x"] = 1
    try:
        assert SALES_RESPONSE_SCHEMA == snapshot
    finally:
        del SALES_RESPONSE_SCHEMA_OFFERINGS["properties"]["signals"]["properties"]["cart"]["x"]


_VARIANT_PAYLOAD = {
    "reply": "The black sneaker is ₦12,000.",
    "intent": "PURCHASE_INTENT",
    "signals": {
        "asked_features": False,
        "requested_demo": False,
        "mentioned_competitor": False,
        "ready_to_buy": True,
        "offer_meeting": False,
        "offer_quote": False,
        "catalog_refs": ["#1"],
        "cart": [{"ref": "#1", "qty": 2}],
        "offered_price": {"ref": "#3", "amount": 40000},
    },
    "captured": {
        "name": "Ada",
        "email": None,
        "company": None,
        "team_size": None,
        "industry": None,
        "plan_interest": None,
    },
}


def test_parse_sales_output_keeps_the_new_signals():
    out = parse_sales_output(json.dumps(_VARIANT_PAYLOAD))
    assert out["reply"].startswith("The black")
    assert out["signals"]["cart"] == [{"ref": "#1", "qty": 2}]
    assert out["intent"] == "PURCHASE_INTENT"


def test_parse_sales_output_tolerates_unknown_top_level_keys():
    out = parse_sales_output(json.dumps({**_VARIANT_PAYLOAD, "surprise": 1}))
    assert set(out) == {"reply", "intent", "signals", "captured"}


def test_score_lead_ignores_the_new_signal_keys():
    lead = SimpleNamespace(email=None, company=None, team_size=None, industry=None, utm={})
    plain = score_lead(lead, {"ready_to_buy": True})
    variant = score_lead(lead, _VARIANT_PAYLOAD["signals"])
    assert plain == variant


@pytest.mark.parametrize(
    "signals, refs, cart, offer",
    [
        (_VARIANT_PAYLOAD["signals"], [1], [(1, 2)], (3, 40000)),
        ({}, [], [], None),
        (None, [], [], None),
        ({"catalog_refs": "#1", "cart": "x", "offered_price": 5}, [], [], None),
        (
            {"catalog_refs": ["#1", "#1", "bad", "#2", "#3", "#4", "#5", "#6"]},
            [1, 2, 3, 4, 5],
            [],
            None,
        ),
        (
            {"cart": [{"ref": "#1", "qty": 0}, {"ref": "#2", "qty": "3"}, {"ref": "#x"}]},
            [],
            [(2, 3)],
            None,
        ),
        ({"cart": [{"ref": "#1", "qty": 1}, {"ref": "#1", "qty": 2}]}, [], [(1, 3)], None),
        ({"offered_price": {"ref": "#2", "amount": -5}}, [], [], None),
        ({"offered_price": {"ref": "#2", "amount": 2500.5}}, [], [], None),
        ({"offered_price": {"ref": "#2", "amount": 2500.0}}, [], [], (2, 2500)),
        ({"cart": [{"ref": "#1", "qty": True}]}, [], [], None),
    ],
)
def test_parse_catalog_signals_normalises_and_drops_garbage(signals, refs, cart, offer):
    parsed = parse_catalog_signals(signals)
    assert parsed.catalog_seqs == refs
    assert [(c.seq, c.qty) for c in parsed.cart] == cart
    assert (
        None
        if parsed.offered_price is None
        else (parsed.offered_price.seq, parsed.offered_price.amount)
    ) == offer


@pytest.mark.asyncio
async def test_generate_sales_response_uses_variant_only_when_asked():
    svc = RAGService(db=AsyncMock())
    seen: dict = {}

    async def fake_raw(messages, *, model, response_format=None, max_tokens=800):
        seen["messages"] = messages
        seen["format"] = response_format
        return json.dumps(_VARIANT_PAYLOAD), 42, False

    prompt = SimpleNamespace(full_prompt="hi")
    config = SimpleNamespace(qualification_questions=[])
    with patch.object(svc, "_call_openai_raw", fake_raw):
        await svc.generate_sales_response(prompt, config)
        assert seen["format"]["json_schema"]["schema"] == SALES_RESPONSE_SCHEMA
        base_system = seen["messages"][0]["content"]

        reply, meta, _, _ = await svc.generate_sales_response(
            prompt, config, catalog_addendum="## Catalog selling\nCART RULES"
        )
    assert seen["format"]["json_schema"]["schema"] == SALES_RESPONSE_SCHEMA_OFFERINGS
    assert seen["messages"][0]["content"].startswith(base_system)
    assert seen["messages"][0]["content"].endswith("CART RULES")
    assert meta["signals"]["cart"] == [{"ref": "#1", "qty": 2}]


def test_an_offered_amount_beyond_the_price_ceiling_is_dropped():
    from app.db.models.offering import MAX_PRICE

    big = parse_catalog_signals({"offered_price": {"ref": "#1", "amount": MAX_PRICE + 1}})
    ok = parse_catalog_signals({"offered_price": {"ref": "#1", "amount": MAX_PRICE}})
    assert big.offered_price is None and ok.offered_price.amount == MAX_PRICE
    assert (
        parse_catalog_signals({"offered_price": {"ref": "#1", "amount": 10**30}}).offered_price
        is None
    )
