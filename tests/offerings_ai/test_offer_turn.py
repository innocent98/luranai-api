"""Review S-B: bargaining turns are answered by code; the model's words never reach the customer."""

import pytest

from app.offerings.offer_turn import is_offer_turn
from app.services.conversation_service import message_channel, voice_channel
from tests.offerings_ai.helpers import (
    add_offering,
    automation_patch,
    make_tenant,
    retrieval_patch,
    send,
)
from tests.offerings_ai.test_cart_flow import GOOD, Model, _cart, _meta, _run, _shop
from tests.offerings_ai.test_chat_guard import ScriptedPlain

AGREEMENTS = [
    "Okay, half price works.",
    "Sure, I can reduce it for you.",
    "Deal!",
    "You asked for 20%. Fine.",
    "We'll honour that.",
    "That's doable!",
    "I'll put it through at your price.",
    "Let's proceed with the red bag at that price.",
    "We fit do am.",
]
BARGAINING = [
    "can I get 10% off the black sneaker?",
    "half price for the black sneaker?",
    "abeg last price for the black sneaker",
    "can you reduce the black sneaker?",
    "I go pay 9k for the black sneaker",
    "Can you do ₦9,000 for the black sneaker?",
]


@pytest.mark.parametrize("message", BARGAINING)
@pytest.mark.parametrize("agreement", AGREEMENTS[:4])
def test_the_models_words_never_reach_a_bargaining_customer(client, db, message, agreement):
    tid = _shop(db)
    r = _run(client, db, tid, Model((agreement, None)), text=message)
    content = r.json()["message"]["content"]
    assert agreement not in content
    assert "The listed price for the Canvas sneaker black is ₦12,000 and it is firm." in content
    assert r.json()["should_escalate"] is False  # a non-negotiable first ask is not a handover


def test_every_agreement_wording_is_dropped_not_filtered(client, db):
    tid = _shop(db)
    for text in AGREEMENTS:
        r = _run(client, db, tid, Model((text, None)), text="I go pay 9k for the black sneaker")
        content = r.json()["message"]["content"]
        assert text not in content
        assert "firm" in content and "[[" not in content


def test_a_negotiable_item_without_a_figure_asks_for_one_and_does_not_escalate(client, db):
    tid = _shop(db)
    r = _run(client, db, tid, Model(("Deal!", None)), text="half price for the leather bag?")
    content = r.json()["message"]["content"]
    assert "open to offers on the Leather bag" in content and "Deal" not in content
    assert r.json()["should_escalate"] is False


def test_an_item_priced_on_request_hands_over(client, db):
    tid = _shop(db)
    add_offering(db, tid, 30, "Custom portrait", price_mode="on_request")  # type: ignore[arg-type]
    r = _run(
        client,
        db,
        tid,
        Model(("Okay!", None, {"ref": "#30", "amount": 5000})),
        text="₦5,000 for the custom portrait?",
    )
    assert "quoted by the team" in r.json()["message"]["content"]
    assert r.json()["should_escalate"] is True


def test_bargaining_with_no_item_in_mind_offers_the_team(client, db):
    tid = _shop(db)
    r = _run(client, db, tid, Model(("Sure thing!", None)), text="abeg last price")
    content = r.json()["message"]["content"]
    assert "can't change prices myself" in content and "Sure thing" not in content
    assert _meta(r)["escalation_action"] == "offer_escalation"


def test_support_mode_gets_code_replies_and_a_consent_offer(client, db):
    tid = make_tenant(db)
    add_offering(db, tid, 1, "Canvas sneaker black", price=12000)
    add_offering(db, tid, 3, "Leather bag", price=45000, negotiable=True)
    model = ScriptedPlain("Deal at ₦9,000!")
    with model.patch(), retrieval_patch(), automation_patch():
        r = send(client, tid, "can you reduce the leather bag?")
    content = r.json()["message"]["content"]
    assert "Deal" not in content and "open to offers on the Leather bag" in content
    assert _meta(r)["escalation_action"] == "offer_escalation"


def test_voice_speaks_the_code_lines_and_waits_for_a_yes(client, db):
    tid = _shop(db)
    marker = voice_channel.set(True)
    try:
        r = _run(
            client,
            db,
            tid,
            Model(("ok", _cart((3, 1)), {"ref": "#3", "amount": 30000})),
            text="₦30,000 for the leather bag?",
        )
    finally:
        voice_channel.reset(marker)
    assert "I've noted your offer of ₦30,000 for the Leather bag." in r.json()["message"]["content"]
    assert _meta(r)["escalation_action"] == "offer_escalation"
    assert r.json()["should_escalate"] is False  # a tool call would stay silent on a call


def test_a_plain_question_is_not_an_offer_turn(client, db):
    tid = _shop(db)
    r = _run(
        client,
        db,
        tid,
        Model(("The black sneaker is [[price:#1]].", None)),
        text="how much is the black sneaker?",
    )
    assert r.json()["message"]["content"] == "The black sneaker is ₦12,000."
    assert _meta(r)["offerings"]["guard"]["checked"] is True


@pytest.mark.parametrize(
    "text, expected",
    [
        ("how much is the black sneaker", False),
        ("what do you have", False),
        ("is there a cheaper one", False),
        ("can you do 10k for it", True),
        ("last price?", True),
        ("20% off?", True),
        ("abeg reduce am", True),
        ("I'll pay 9k", True),
        ("my budget is 20k", False),
    ],
)
def test_offer_turn_detection(db, text, expected):
    from tests.offerings_ai.test_guard import ITEMS, _grounding

    g = _grounding(items=ITEMS, matched=(1,))
    assert is_offer_turn(text, g, False) is expected
    assert is_offer_turn("anything", g, True) is True  # the model returned an offered price


def test_a_good_non_bargaining_reply_is_untouched(client, db):
    tid = _shop(db)
    r = _run(client, db, tid, Model((GOOD, _cart((1, 2)))), text="I want two black sneakers")
    assert "Noted: two Canvas sneaker black" in r.json()["message"]["content"]
    assert "[[" not in r.json()["message"]["content"]


def test_channels_do_not_change_the_rule(client, db):
    tid = _shop(db)
    marker = message_channel.set("whatsapp")
    try:
        r = _run(
            client, db, tid, Model(("Deal!", None)), text="abeg last price for the black sneaker"
        )
    finally:
        message_channel.reset(marker)
    assert "Deal" not in r.json()["message"]["content"]
