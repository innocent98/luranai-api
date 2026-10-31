"""Car dealer, bakery, electronics and real estate (final review): customer messages must not read
as offers, and ordinary replies must not be blocked. Each set at most 3% wrong."""

from uuid import uuid4

import pytest

from app.db.models.offering import OfferingAvailability, OfferingKind, OfferingPriceMode
from app.offerings.facts import OfferingFacts
from app.offerings.grounding import GroundingResult
from app.offerings.guard import check_reply
from app.offerings.offer_turn import is_offer_turn
from tests.offerings_ai import lead7_sets as S

M, A, K = OfferingPriceMode, OfferingAvailability, OfferingKind


def item(seq, name, price, mode=M.FIXED, av=A.IN_STOCK, kind=K.PRODUCT, neg=False, desc=""):
    return OfferingFacts(
        id=uuid4(), seq=seq, name=name, kind=kind, category=None, description=desc, price_mode=mode,
        price=price, price_note=None, availability=av, quantity=None, negotiable=neg,
    )  # fmt: skip


CARS = [
    item(1, "Toyota Camry 2018", 14500000, neg=True, desc="2.5L engine, 62,000 km, foreign used"),
    item(2, "Honda Accord 2016", 9800000, desc="2.4L, 88,000 km"),
    item(3, "Lexus RX 350 2015", 18000000, mode=M.FROM),
    item(4, "Toyota Corolla 2012", 6200000, av=A.OUT_OF_STOCK),
    item(5, "Car financing", None, mode=M.ON_REQUEST, kind=K.SERVICE),
]
BAKE = [
    item(1, "Chocolate cake 1kg", 18000), item(2, "Red velvet cake", 15000, mode=M.FROM),
    item(3, "Meat pie", 800), item(4, "Sausage roll pack of 6", 3000),
    item(5, "Wedding cake", None, mode=M.ON_REQUEST), item(6, "Banana bread 500g", 4500),
]  # fmt: skip
ELEC = [
    item(1, "MacBook Air M2 2022", 1250000), item(2, "iPhone 13 128GB", 650000),
    item(3, "Samsung A1500 TV", 420000), item(4, "HP EliteBook 840 G5", 380000),
]  # fmt: skip
REAL = [
    item(1, "3 bedroom flat Lekki", 4500000, neg=True),
    item(2, "Duplex Ajah", 120000000, neg=True),
]  # fmt: skip


def grounding(items, matched, mode="detail"):
    return GroundingResult(
        mode=mode, block=" ".join(i.description for i in items), items=items,
        matched_seqs=list(matched), catalog_names=[i.name for i in items],
    )  # fmt: skip


MESSAGE_SETS = [
    ("cars", CARS, S.CAR_MSGS),
    ("bakery", BAKE, S.BAKE_MSGS),
    ("electronics", ELEC, S.EL_MSGS),
    ("real estate", REAL, S.RE_MSGS),
]


@pytest.mark.parametrize("label,items,msgs", MESSAGE_SETS, ids=[m[0] for m in MESSAGE_SETS])
@pytest.mark.parametrize("matched", [True, False], ids=["matched", "unmatched"])
def test_ordinary_customer_messages_are_not_offers(label, items, msgs, matched):
    g = grounding(items, [1] if matched else [], "detail" if matched else "none")
    wrong = [m for m in msgs if is_offer_turn(m, g, False)]
    assert len(wrong) / len(msgs) <= 0.03, wrong


@pytest.mark.parametrize(
    "label,items,rs,kb,customer",
    [
        ("cars", CARS, S.CAR_REPLIES, S.CAR_KB, ["how much is the camry", "ok tell me more"]),
        (
            "bakery",
            BAKE,
            S.BAKE_REPLIES,
            S.BAKE_KB,
            ["how much is the chocolate cake", "and delivery?"],
        ),
    ],
    ids=["cars", "bakery"],
)
def test_ordinary_replies_are_not_blocked(label, items, rs, kb, customer):
    bad = []
    for matched, reply in rs:
        v = check_reply(reply, grounding(items, matched), customer_texts=customer, kb_text=kb)
        if not v.ok:
            bad.append((reply, [x.reason for x in v.violations]))
    assert len(bad) / len(rs) <= 0.03, bad


def test_a_year_in_the_customer_message_does_not_arm_the_agreement_rules():
    g = grounding(ELEC, [1])
    for cust in ("MacBook Air M2 2022 price?", "is the macbook the 2022 model?"):
        v = check_reply(
            "Yes, the MacBook Air M2 2022 is available at [[price:#1]].",
            g,
            customer_texts=[cust],
        )
        assert v.ok, [(x.reason, x.sentence) for x in v.violations]


def test_a_model_year_in_a_reply_is_not_a_literal_amount():
    g = grounding(CARS, [1])
    for reply in (
        "It's a 2016 model.",
        "The car is a 2018 model with one owner.",
        "It was made in 2018, registered 2019.",
        "Model year 2017, 62,000 km.",
    ):
        v = check_reply(reply, g, customer_texts=["tell me about it"])
        assert v.ok, (reply, [(x.reason) for x in v.violations])


SHOP = [
    item(1, "Black sneaker", 12000), item(2, "White sneaker", 13500),
    item(3, "Leather bag", 45000, neg=True),
]  # fmt: skip


@pytest.mark.parametrize("matched", [True, False], ids=["matched", "unmatched"])
def test_the_fresh_haggling_set_is_caught(matched):
    g = grounding(SHOP, [1] if matched else [], "detail" if matched else "none")
    missed = [m for m in S.HAGGLING_7 if not is_offer_turn(m, g, False)]
    assert 1 - len(missed) / len(S.HAGGLING_7) >= 0.90, missed


@pytest.mark.parametrize("matched", [True, False], ids=["matched", "unmatched"])
def test_the_fresh_ordinary_set_is_left_alone(matched):
    g = grounding(SHOP, [1] if matched else [], "detail" if matched else "none")
    wrong = [m for m in S.ORDINARY_7 if is_offer_turn(m, g, False)]
    assert len(wrong) / len(S.ORDINARY_7) <= 0.03, wrong


def test_discount_promises_without_a_figure_are_blocked_even_when_unanchored():
    g = grounding(SHOP, [1])
    for reply in (
        "I can give you a discount on it.",
        "We can give you a discount if you buy today.",
        "I'll reduce it for you.",
        "Okay, I can do it for less.",
        "Okay, done. I've applied your discount.",
        "Sure, you can have both for that price.",
    ):
        v = check_reply(reply, g, customer_texts=["how much is the black sneaker"])
        assert not v.ok, reply


def test_a_discount_the_knowledge_base_states_may_be_repeated():
    g = grounding(SHOP, [1])
    kb = "Students get a 10% discount on orders above ₦50,000 with a valid ID."
    v = check_reply(
        "Students get a 10% discount on orders above ₦50,000 with a valid ID.",
        g,
        customer_texts=["is there student pricing?"],
        kb_text=kb,
    )
    assert v.ok, [(x.reason, x.sentence) for x in v.violations]


def test_a_kb_discount_sentence_may_not_be_stretched_into_a_new_promise():
    g = grounding(SHOP, [1])
    kb = "Students get a 10% discount on orders above ₦50,000 with a valid ID."
    for reply in (
        "Students get a 10% discount on orders above ₦50,000 with a valid ID, and you get 20% too.",
        "Students get a 15% discount on orders above ₦50,000 with a valid ID.",
        "Students get a 10% discount, and I can give you a discount on the black sneaker as well.",
    ):
        v = check_reply(reply, g, customer_texts=["is there student pricing?"], kb_text=kb)
        assert not v.ok, reply


def test_a_price_claim_below_the_list_is_an_offer_even_phrased_as_a_check():
    g = grounding(SHOP, [1])
    assert is_offer_turn("Your Instagram says 9,000, is that correct?", g, False)
    assert not is_offer_turn("Your Instagram says 12,000, is that correct?", g, False)
