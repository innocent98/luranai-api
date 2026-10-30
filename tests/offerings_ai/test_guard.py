"""T4: the price guard verdict. Table-driven over reply strings (spec 7.5, decision D1).

Catalog under test (injected this turn): #1 Canvas sneaker black ₦12,000 · #2 Canvas sneaker white
₦13,500 · #3 Leather bag ₦45,000 (negotiable) · #4 Phone repair from ₦5,000 per phone ·
#5 Sold out sandals ₦8,000 (sold out) · #6 Custom portrait (price on request).
"""

from dataclasses import dataclass
from uuid import uuid4

import pytest

from app.db.models.offering import (
    OfferingAvailability,
    OfferingKind,
    OfferingPriceMode,
)
from app.offerings.facts import OfferingFacts
from app.offerings.grounding import GroundingResult
from app.offerings.guard import check_reply


def _item(seq, name, price, **kw):
    return OfferingFacts(
        id=uuid4(),
        seq=seq,
        name=name,
        kind=kw.pop("kind", OfferingKind.PRODUCT),
        category=None,
        description="",
        price_mode=kw.pop("price_mode", OfferingPriceMode.FIXED),
        price=price,
        price_note=kw.pop("price_note", None),
        availability=kw.pop("availability", OfferingAvailability.IN_STOCK),
        quantity=None,
        negotiable=kw.pop("negotiable", False),
    )


ITEMS = [
    _item(1, "Canvas sneaker black", 12000),
    _item(2, "Canvas sneaker white", 13500),
    _item(3, "Leather bag", 45000, negotiable=True),
    _item(
        4,
        "Phone repair",
        5000,
        price_mode=OfferingPriceMode.FROM,
        kind=OfferingKind.SERVICE,
        price_note="per phone",
    ),
    _item(5, "Sold out sandals", 8000, availability=OfferingAvailability.OUT_OF_STOCK),
    _item(6, "Custom portrait", None, price_mode=OfferingPriceMode.ON_REQUEST),
    _item(7, "Puff puff", 900),
    _item(8, "Toyota Camry 2015", 4500000),
    _item(9, "N95 Mask", 1500),
]


def _grounding(items=ITEMS, *, strict=False, currency="NGN", matched=()):
    return GroundingResult(
        mode="unavailable" if strict else "detail",
        block="",
        items=[] if strict else list(items),
        matched_seqs=list(matched),
        strict=strict,
        currency=currency,
    )


@dataclass
class Case:
    reply: str
    ok: bool
    reason: str | None = None  # expected violation reason when not ok
    customer: tuple = ()
    kb: str = ""
    tiers: tuple = ()
    strict: bool = False
    currency: str = "NGN"
    matched: tuple = ()


C = Case
CASES = [
    # --- accepted ------------------------------------------------------------------------
    C("The black sneaker is ₦12,000 and it is in stock.", True),
    C("The black sneaker is N12,000.", True),
    C("The black sneaker is 12,000 naira.", True),
    C("The black sneaker is ₦12000.", True),
    C("The black sneaker is 12k.", True),
    C("The black sneaker is ₦12,000.00.", True),
    C("Our black sneaker costs twelve thousand naira.", True),
    C("Phone repair starts from ₦5,000 per phone.", True),
    C("The leather bag is ₦45,000.", True),
    C("The black sneaker is ₦12,000 and the white one is ₦13,500.", True),
    C("Canvas sneaker black: ₦12,000\nCanvas sneaker white: ₦13,500", True),
    C(
        "#1 Canvas sneaker black ₦12,000\n#2 Canvas sneaker white ₦13,500\n#3 Leather bag ₦45,000",
        True,
    ),
    C("Prices run from ₦5,000 up to ₦45,000 across our list.", True),
    C("The white sneaker costs ₦13,500, which is more than the black one at ₦12,000.", True),
    C("We have 2 pairs of the white sneaker in size 42.", True),
    C("Which size do you need, 41 or 42?", True),
    C("Item #3 is the leather bag.", True),
    C("We opened in 2026 and reply 9:00 to 17:30.", True),
    C("Call us on 08012345678 for the team.", True),
    C("I've noted your request; the team will confirm.", True),
    C("The custom portrait is priced on request; I can connect you with the team.", True),
    C(
        "You said ₦20,000. The leather bag is listed at ₦45,000.",
        True,
        customer=("my budget is ₦20,000",),
    ),
    C("With a 20k budget, the black sneaker at ₦12,000 fits.", True, customer=("budget is 20k",)),
    C("Delivery within Lagos is ₦2,000.", True, kb="Delivery in Lagos costs ₦2,000 per order."),
    C(
        "The black sneaker is ₦12,000. Delivery within Lagos is ₦2,000.",
        True,
        kb="Delivery in Lagos costs ₦2,000 per order.",
    ),
    C("Our Pro plan is ₦30,000 per month.", True, tiers=(30000,)),
    C("The black sneaker is ₦12,000.", True, currency="NGN"),
    C("The black sneaker is $12,000.", True, currency="USD"),
    C("Which item would you like?", True),
    # --- haggling and concessions (D1) --------------------------------------------------
    C("I can do ₦10,000 for the black sneaker.", False, "concession"),
    C("Sure, ₦10,000 works for the leather bag.", False, "concession"),
    C("I can give you ₦2,000 off the leather bag.", False, "concession"),
    C("We can do 2k off.", False, "concession"),
    C(
        "Deal at ₦8,000 for the black sneaker.",
        False,
        "concession",
        customer=("I will pay ₦8,000",),
    ),
    C("The leather bag now goes for ₦40,000.", False, "wrong_item_price"),
    C(
        "Okay, I accept your ₦30,000 offer for the leather bag.",
        False,
        "concession",
        customer=("I'll pay ₦30,000 for the bag",),
    ),
    C("The lowest I can go is ₦11,000 for the black sneaker.", False, "concession"),
    C("Final price is ₦11,500.", False, "concession"),
    C("I can offer a 10% discount on the black sneaker.", False, "percent"),
    C("That works out to 5 percent off.", False, "percent"),
    C("Meet you at ₦9,000 for the black sneaker?", False, "concession", customer=("9k?",)),
    C("The owner is open to offers on the leather bag.", True),
    C(
        "I've noted your offer of ₦30,000 for the leather bag; the team will follow up.",
        False,
        "customer_figure_below_price",
        customer=("can you do ₦30,000 for the bag",),
    ),
    C(
        "Fine, ₦8,000 it is.",
        False,
        "concession",
        customer=("can you do ₦8,000?",),
    ),
    C(
        "Alright, ₦30,000 then.",
        False,
        "concession",
        customer=("I'll pay ₦30,000",),
    ),
    C(
        "₦8,000 works for the black sneaker.",
        False,
        "concession",
        customer=("8k?",),
    ),
    C(
        "That is agreeable at ₦30,000 for the leather bag.",
        False,
        "concession",
        customer=("how about ₦30,000",),
    ),
    C(
        "You offered ₦30,000, but the leather bag is listed at ₦45,000.",
        False,
        "customer_figure_below_price",
        customer=("how about ₦30,000",),
    ),
    # --- review findings: concessions at a coincidentally listed price, echoed-then-accepted ---
    C("I can do ₦12,000 for you.", False, "concession"),
    C("Sure, ₦12,000 for you.", False, "concession"),
    C(
        "I can do ₦12,000 for you.",
        False,
        "concession",
        customer=("can you do 12k for the bag",),
    ),
    C("We have a deal at ₦13,500.", False, "concession"),
    C(
        "You offered ₦30,000 for the leather bag, that works for me.",
        False,
        "concession",
        customer=("how about ₦30,000 for the bag",),
    ),
    C(
        "You offered ₦30,000 for the leather bag, so okay.",
        False,
        "concession",
        customer=("how about ₦30,000 for the bag",),
    ),
    C(
        "You offered ₦30,000, sounds fine to me.",
        False,
        "concession",
        customer=("how about ₦30,000",),
    ),
    # Agreement in a separate sentence after the customer's below-list figure is echoed.
    C(
        "You offered ₦30,000. Sounds fine to me.",
        False,
        "agreement_after_offer",
        customer=("how about ₦30,000",),
    ),
    C("I can do ₦12,000 for the black sneaker.", True),
    C("The black sneaker is ₦12,000 and there is no discount.", True),
    C("I can't offer a discount on the leather bag; it is ₦45,000.", True),
    C(
        "Thanks for the thought. Anyway, ₦30,000 is what you said.",
        True,
        customer=("how about ₦30,000",),
    ),
    # --- found by the eval: a percentage the customer named must not be accepted back ---------
    C(
        "I can offer 20% off the black sneaker.",
        False,
        "percent",
        customer=("give me 20% discount",),
    ),
    C("Sure, 10 percent off then.", False, "percent", customer=("can I get 10 percent off",)),
    C(
        "You asked for 20% off, but the black sneaker is listed at ₦12,000.",
        False,
        "percent",
        customer=("give me 20% discount",),
    ),
    C(
        "I can't offer a 20% discount on the black sneaker.",
        False,
        "percent",
        customer=("give me 20% discount",),
    ),
    # --- second independent review (Opus) -------------------------------------------------
    C(
        "Yes, the black sneaker is ₦10,000 as you said.",
        False,
        "customer_figure_below_price",
        customer=("10,000",),
    ),
    C(
        "As you said, ₦10,000 for the black sneaker.",
        False,
        "customer_figure_below_price",
        customer=("10,000",),
    ),
    C(
        "Black sneaker: your price ₦10,000 confirmed.",
        False,
        "customer_figure_below_price",
        customer=("10,000",),
    ),
    C(
        "Within your budget, the black sneaker is ₦9,000.",
        False,
        "customer_figure_below_price",
        customer=("budget 9k",),
    ),
    C(
        "You offered ₦10,000 for the black sneaker. Okay, deal!",
        False,
        "customer_figure_below_price",
        customer=("10,000",),
    ),
    C(
        "I'll pass your offer of ₦10,000 to the owner. Good news, the owner has agreed!",
        False,
        "agreement_after_offer",
        customer=("10,000",),
    ),
    C("I'll pass your offer of ₦10,000 to the owner.", True, customer=("10,000",)),
    C("Sure, 10% off the black sneaker for you.", False, "percent", customer=("10% off",)),
    C("Okay, I can do ₦9,000 for you.", False, "concession", kb="Black sneaker ₦9,000"),
    C("The black sneaker is ₦11,000only.", False, "wrong_item_price"),
    C("The black sneaker is ₦11,000ish.", False, "wrong_item_price"),
    C("Okay, 800 works for the puff puff.", False),
    C("Puff puff, 800 only.", False, "wrong_item_price"),
    C("Puff puff, 900 only.", True),
    C("The black sneaker is fifteen hundred naira.", False, "wrong_item_price"),
    C("The black sneaker is fifteen k.", False, "wrong_item_price"),
    C("The black sneaker is 5 grand.", False, "wrong_item_price"),
    C("The black sneaker is 12 thousand five hundred.", False, "wrong_item_price"),
    C("The black sneaker is 12 thousand.", True),
    C("Black sneaker is half price today.", False, "discount_wording"),
    C("You can have the black sneaker for free.", False, "discount_wording"),
    C("I'll take 500 off the black sneaker.", False),
    C("I can't offer a discount on the black sneaker.", True),
    C("The black sneaker is cheaper today.", False, "discount_wording"),
    C("It's ₦13,500.", False, "not_in_block", matched=(1,)),
    C("It's ₦12,000.", True, matched=(1,)),
    C("The Toyota Camry 2015 is ₦4,500,000.", True),
    C("The N95 Mask is ₦1,500.", True),
    C("Call 0803 123 4567 for the team.", True),
    C("We are open from 31/10/2026.", True),
    C("Your reference is LUR-2041.", True),
    C("Delivery is 2,5k.", True, kb="Delivery costs ₦2,500."),
    C("Delivery is 2,5k.", False, "not_in_block"),
    C("The black sneaker is ₦30,000 on our Pro plan.", False, "wrong_item_price", tiers=(30000,)),
    C("Okay, I can do ₦30,000 for you.", False, "concession", tiers=(30000,)),
    # --- automated review of fa7ce37 -----------------------------------------------------------
    C("The black sneaker is NGN-11000.", False, "wrong_item_price"),
    C("The black sneaker is USD-12.", False),
    C("Your reference is LUR-2041.", True),
    C("The black sneaker and the leather bag are ₦20,000 each.", False,
      "customer_figure_below_price", customer=("20k each?",)),
    C("I can't give a discount, but the black sneaker is half price today.", False, "discount_wording"),
    C("No discount today; the black sneaker is for free.", False, "discount_wording"),
    C("I can't offer a discount on the black sneaker.", True),
    C("There is no discount on the black sneaker.", True),
    # --- wrong, invented and stale prices ------------------------------------------------
    C("The black sneaker is ₦11,000.", False, "wrong_item_price"),
    C("The black sneaker is ₦12,500.", False, "wrong_item_price"),
    C("The black sneaker is 1,200.", False, "wrong_item_price"),
    C("The black sneaker is ₦120,000.", False, "wrong_item_price"),
    C("The black sneaker is 12 thousand five hundred naira.", False, "wrong_item_price"),
    C("The black sneaker is N2,500.50.", False, "wrong_item_price"),
    C("The black sneaker is ₦12,000.50.", False, "wrong_item_price"),
    C("The black sneaker is about 12.5k.", False, "wrong_item_price"),
    C("The white sneaker is ₦12,000.", False, "wrong_item_price"),
    C("The leather bag is ₦13,500.", False, "wrong_item_price"),
    C("Phone repair is ₦8,000 per phone.", False, "wrong_item_price"),
    C("The sold out sandals are ₦9,000.", False, "wrong_item_price"),
    C("The custom portrait is ₦20,000.", False, "wrong_item_price"),
    C("Phone repair starts from ₦3,000.", False, "wrong_item_price"),
    C("Sneakers range between 10k and 12k.", False, "wrong_item_price"),
    C("The black sneaker is NGN 12,500.", False, "wrong_item_price"),
    C("The black sneaker was ₦15,000 but is now ₦12,000.", False, "wrong_item_price"),
    C("Our sneakers cost ₦9,000.", False, "wrong_item_price", kb="Sneakers sell at ₦9,000 each."),
    C("Phone repair is ₦12,000 per phone.", False, "wrong_item_price"),
    # --- totals the model must not compute ----------------------------------------------
    C("2 x ₦12,000 = ₦24,000 for two black sneakers.", False, "wrong_item_price"),
    C("Two black sneakers come to 24k in total.", False, "wrong_item_price"),
    C("Your total is ₦57,000.", False, "not_in_block"),
    C("That is ₦25,500 for one black and one white sneaker.", False, "wrong_item_price"),
    # --- no item named: only KB / customer / tier amounts pass --------------------------
    C("Delivery is ₦3,000.", False, "not_in_block"),
    C("Delivery is ₦3,000.", True, kb="Delivery fee: ₦3,000."),
    C("Our warranty costs ₦1,750.", False, "not_in_block", kb="Delivery fee: ₦3,000."),
    C(
        "The black sneaker is ₦12,000, plus ₦2,000 delivery.",
        False,
        "kb_beside_item",
        kb="Delivery in Lagos costs ₦2,000 per order.",
    ),
    C(
        "Delivery for the leather bag is ₦3,000.",
        False,
        "kb_beside_item",
        kb="Delivery fee: ₦3,000.",
    ),
    C("You said ₦50,000 earlier.", True, customer=("my budget is ₦50,000",)),
    C("Pro is ₦40,000 a month.", False, "not_in_block", tiers=(30000,)),
    # --- currency ------------------------------------------------------------------------
    C("The black sneaker is $12,000.", False, "currency", currency="NGN"),
    C("The black sneaker is 12,000 dollars.", False, "currency"),
    C("The black sneaker is ₦12,000.", False, "currency", currency="USD"),
    C("The black sneaker is USD 12.", False, "currency"),
    # --- obfuscation and format tricks ----------------------------------------------------
    C("The black sneaker is ₦ 12 000.", True),
    C("The black sneaker is ₦ 11 000.", False, "wrong_item_price"),
    C("The black sneaker is ₦12,0 00.", False, "wrong_item_price"),
    C("The black sneaker is N 11k.", False, "wrong_item_price"),
    C("Black sneaker: eleven thousand naira.", False, "wrong_item_price"),
    C("It's ₦10,000", False, "not_in_block"),
    # --- strict mode (lookup failed) ------------------------------------------------------
    C("I cannot check the list right now. Shall I connect you with the team?", True, strict=True),
    C("The black sneaker is ₦12,000.", False, "strict", strict=True),
    C("Delivery is ₦2,000.", False, "strict", strict=True, kb="Delivery is ₦2,000."),
    C("You said ₦20,000.", False, "strict", strict=True, customer=("₦20,000",)),
    C("That is 10% off.", False, "strict", strict=True),
]


@pytest.mark.parametrize(
    "case", CASES, ids=[f"{i:03d}-{c.reply[:48]}" for i, c in enumerate(CASES)]
)
def test_guard_verdict(case: Case):
    verdict = check_reply(
        case.reply,
        _grounding(strict=case.strict, currency=case.currency, matched=case.matched),
        customer_texts=case.customer,
        kb_text=case.kb,
        tier_amounts=case.tiers,
    )
    assert verdict.ok is case.ok, [(v.reason, v.sentence) for v in verdict.violations]
    if not case.ok and case.reason is not None:
        assert case.reason in {v.reason for v in verdict.violations}, [
            (v.reason, v.sentence) for v in verdict.violations
        ]


def test_at_least_sixty_reply_strings():
    assert len(CASES) >= 60


def test_violation_reports_the_sentence_and_amount():
    v = check_reply("Hello. The black sneaker is ₦11,000. Bye.", _grounding())
    assert not v.ok
    assert v.violations[0].sentence == "The black sneaker is ₦11,000."
    assert v.violations[0].amounts == [11000]


def test_empty_reply_is_ok():
    assert check_reply("", _grounding()).ok


def test_amount_inside_decimal_is_not_split_into_sentences():
    assert check_reply("The black sneaker is ₦12,000.00. Anything else?", _grounding()).ok
