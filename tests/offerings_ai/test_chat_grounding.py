"""T2: the block reaches the model on every channel, and a catalog hit counts as grounded."""

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.offerings import grounding
from app.services.conversation_service import message_channel
from tests.offerings_ai.helpers import (
    add_offering,
    automation_patch,
    make_tenant,
    retrieval_patch,
    send,
)

_SALES_REPLY = json.dumps(
    {
        "reply": "The black sneaker is ₦12,000.",
        "intent": "PRICING_INQUIRY",
        "signals": {
            "asked_features": False,
            "requested_demo": False,
            "mentioned_competitor": False,
            "ready_to_buy": False,
            "offer_meeting": False,
            "offer_quote": False,
            "catalog_refs": ["#1"],
            "cart": [],
            "offered_price": None,
        },
        "captured": {
            "name": None,
            "email": None,
            "company": None,
            "team_size": None,
            "industry": None,
            "plan_interest": None,
        },
    }
)


def _shop(db, **kw):
    tid = make_tenant(db, **kw)
    add_offering(db, tid, 1, "Canvas sneaker black", price=12000, category="Sneakers")
    add_offering(db, tid, 2, "Canvas sneaker white", price=13500, category="Sneakers")
    return tid


class Plain:
    """Records what the plain model call receives and answers with a fixed reply."""

    def __init__(self, reply="The black sneaker is ₦12,000."):
        self.prompts: list[str] = []
        self.reply = reply

    def patch(self):
        async def fake(
            _self, prompt, max_tokens=800, model_name=None, system_message=None, image_urls=None
        ):
            self.prompts.append(prompt.full_prompt)
            return self.reply, 120, False

        return patch("app.services.rag_service.RAGService.generate_ai_response", new=fake)


class Sales:
    def __init__(self):
        self.calls: list[dict] = []

    def patch(self):
        async def fake(_self, prompt, config, **kwargs):
            self.calls.append({"prompt": prompt.full_prompt, **kwargs})
            return (
                "The black sneaker is ₦12,000.",
                {
                    "intent": "PRICING_INQUIRY",
                    "captured": {},
                    "signals": json.loads(_SALES_REPLY)["signals"],
                },
                150,
                False,
            )

        return patch("app.services.rag_service.RAGService.generate_sales_response", new=fake)


def test_block_is_appended_to_the_plain_prompt(client, db):
    tid = _shop(db)
    plain = Plain()
    with plain.patch(), retrieval_patch(), automation_patch():
        r = send(client, tid, "how much is the black sneaker?")
    assert r.status_code == 200, r.text
    prompt = plain.prompts[0]
    assert '<offering ref="#1">' in prompt and "₦12,000" in prompt
    assert prompt.index("Runtime policy decision") < prompt.index("## Offerings catalog")


def test_catalog_hit_counts_as_retrieval(client, db):
    tid = _shop(db)
    plain = Plain()
    with plain.patch(), retrieval_patch(), automation_patch():
        send(client, tid, "how much is the black sneaker?")
    assert "Required action: answer" in plain.prompts[0]
    assert "Reason: normal_generation" in plain.prompts[0]


def test_browse_counts_as_retrieval(client, db):
    tid = _shop(db)
    plain = Plain()
    with plain.patch(), retrieval_patch(), automation_patch():
        send(client, tid, "what do you have?")
    assert "Required action: answer" in plain.prompts[0]


def test_no_match_keeps_the_clarify_policy_and_says_not_listed(client, db):
    tid = _shop(db)
    plain = Plain()
    with plain.patch(), retrieval_patch(), automation_patch():
        send(client, tid, "do you sell helicopters")
    assert "Required action: clarify" in plain.prompts[0]
    assert "Nothing listed matches" in plain.prompts[0]


def test_sales_mode_gets_the_catalog_addendum(client, db):
    tid = _shop(db, sales_enabled=True)
    sales = Sales()
    with sales.patch(), retrieval_patch(), automation_patch():
        r = send(client, tid, "how much is the black sneaker?")
    assert r.status_code == 200, r.text
    call = sales.calls[0]
    assert '<offering ref="#1">' in call["prompt"]
    assert "signals.cart" in call["catalog_addendum"]


def test_sales_tenant_without_items_gets_no_extra_kwarg(client, db):
    tid = make_tenant(db, sales_enabled=True)
    sales = Sales()
    with sales.patch(), retrieval_patch(), automation_patch():
        send(client, tid, "how much is the black sneaker?")
    assert "catalog_addendum" not in sales.calls[0]


def test_whatsapp_channel_adds_plain_text_formatting(client, db):
    tid = _shop(db)
    plain = Plain()
    marker = message_channel.set("whatsapp")
    try:
        with plain.patch(), retrieval_patch(), automation_patch():
            send(client, tid, "how much is the black sneaker?")
    finally:
        message_channel.reset(marker)
    assert "WhatsApp" in plain.prompts[0] and "no markdown" in plain.prompts[0].lower()

    web = Plain()
    with web.patch(), retrieval_patch(), automation_patch():
        send(client, tid, "how much is the black sneaker?", user="v2")
    assert "WhatsApp" not in web.prompts[0]


def test_followup_uses_the_previous_customer_message(client, db):
    tid = _shop(db)
    for i in range(3, 20):
        add_offering(db, tid, i, f"Filler thing {i}", price=900 + i)
    plain = Plain()
    with plain.patch(), retrieval_patch(), automation_patch():
        r1 = send(client, tid, "I am looking for the black sneaker")
        sid = r1.json()["session_id"]
        send(client, tid, "how much?", session_id=sid)
    assert '<offering ref="#1">' in plain.prompts[1]


def test_block_tokens_are_counted_in_the_prompt_breakdown(client, db):
    tid = _shop(db)
    plain = Plain()
    with plain.patch(), retrieval_patch(), automation_patch():
        r = send(client, tid, "how much is the black sneaker?")
    meta = r.json()["message"]["meta"]
    from app.utils.token_counter import count_tokens

    block = plain.prompts[0].split("## Offerings catalog", 1)[1]
    assert meta["token_breakdown"]["prompt_tokens"] >= count_tokens(block) - 20


def test_lookup_failure_still_answers_with_the_fail_closed_block(client, db):
    tid = _shop(db, sales_enabled=True)
    sales = Sales()
    with (
        patch.object(grounding, "_search", side_effect=RuntimeError("boom")),
        sales.patch(),
        retrieval_patch(),
        automation_patch(),
    ):
        r = send(client, tid, "how much is the black sneaker?")
    assert r.status_code == 200, r.text
    assert "unavailable" in sales.calls[0]["prompt"]
    assert "<offering ref=" not in sales.calls[0]["prompt"]
    assert "catalog_addendum" not in sales.calls[0]


@pytest.mark.parametrize("sales", [False, True])
def test_tenant_isolation_in_the_prompt(client, db, sales):
    mine = make_tenant(db, sales_enabled=sales)
    add_offering(db, mine, 1, "Red hat", price=2000)
    other = _shop(db, sales_enabled=sales)  # noqa: F841
    rec = Sales() if sales else Plain()
    with rec.patch(), retrieval_patch(), automation_patch():
        send(client, mine, "how much is the black sneaker?")
    text = rec.calls[0]["prompt"] if sales else rec.prompts[0]
    assert "Canvas" not in text and "12,000" not in text


def test_ns(db):
    assert SimpleNamespace  # keep import used
