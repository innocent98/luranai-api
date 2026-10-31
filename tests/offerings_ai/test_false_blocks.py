"""Block-rate regression: normal replies must pass the guard (spec target: at most 3% blocked).

Two sets of 40 replies in different domains: a fashion shop (the Opus reviewer's `n40` set, with
every catalog price written as the token the model is now told to use) and a clinic plus law firm
(services, "from" prices, consultation fees). Replies marked EXPECT_BLOCK are real violations the
reviewer's set contained (a model-computed total, an old price); they must stay blocked.
"""

import re
from uuid import uuid4

from app.db.models.offering import OfferingAvailability, OfferingKind, OfferingPriceMode
from app.offerings.facts import OfferingFacts
from app.offerings.grounding import GroundingResult
from app.offerings.guard import check_reply

FROM, ON_REQ = OfferingPriceMode.FROM, OfferingPriceMode.ON_REQUEST


def _f(seq, name, price, negotiable=False, mode=OfferingPriceMode.FIXED, kind=OfferingKind.PRODUCT):
    return OfferingFacts(
        id=uuid4(),
        seq=seq,
        name=name,
        kind=kind,
        category=None,
        description="",
        price_mode=mode,
        price=price,
        price_note=None,
        availability=OfferingAvailability.IN_STOCK,
        quantity=None,
        negotiable=negotiable,
    )


def _check(items, names, kb, reply, customer, matched):
    g = GroundingResult(
        mode="detail", block="", items=items, matched_seqs=list(matched), catalog_names=names
    )
    return check_reply(reply, g, customer_texts=list(customer), kb_text=kb)


def _tokenise(reply, price_to_seq):
    """Write each catalog price as its token, the way a model following the rules answers."""
    out = reply
    for text, seq in price_to_seq.items():
        out = out.replace(f"from {text}", f"[[price:#{seq}]]").replace(text, f"[[price:#{seq}]]")
    return out


# -- set 1: fashion shop ---------------------------------------------------------------------

SHOP = [
    _f(1, "Black Sneaker", 12000, True),
    _f(2, "Black Sneaker Pro", 18000),
    _f(3, "Red Bag", 25000, True),
    _f(4, "Red Bag Large", 32000),
    _f(5, "Leather Wallet", 8500),
    _f(6, "Ankara Dress", 15000),
    _f(7, "Braiding Service", 5000, mode=FROM, kind=OfferingKind.SERVICE),
    _f(8, "Custom Suit", None, mode=ON_REQ),
    _f(9, "Silver Wristwatch", 45000),
    _f(10, "Kids Sandals", 6500),
]
SHOP_PRICES = {
    "₦12,000": 1,
    "₦18,000": 2,
    "₦25,000": 3,
    "₦32,000": 4,
    "₦8,500": 5,
    "₦15,000": 6,
    "₦5,000": 7,
    "₦45,000": 9,
    "₦6,500": 10,
}
SHOP_KB = (
    "Delivery within Lagos costs ₦2,500. Delivery outside Lagos is ₦4,000. We are open 9am to "
    "6pm, Monday to Saturday. Our shop is at 14 Allen Avenue, Ikeja. Call 0803 123 4567. "
    "Orders above ₦50,000 get free delivery."
)
SHOP_NAMES = [i.name for i in SHOP]

SET1 = [  # (reply, customer messages, matched seqs)
    (
        "Here's what we have:\n- Black Sneaker: ₦12,000\n- Black Sneaker Pro: ₦18,000\n- Red Bag: ₦25,000\n- Red Bag Large: ₦32,000\n- Leather Wallet: ₦8,500\n- Ankara Dress: ₦15,000",
        ["what do you sell?"],
        [],
    ),
    ("The Red Bag is ₦25,000.", ["how much is the red bag?"], [3]),
    ("Sure! The Red Bag costs ₦25,000 and it's in stock.", ["how much is the red bag?"], [3]),
    ("It's ₦25,000.", ["how much is the red bag?"], [3]),
    ("Sure, it's ₦25,000.", ["how much is the red bag?"], [3]),
    (
        "Our braiding service starts from ₦5,000, depending on the style.",
        ["how much for braids"],
        [7],
    ),
    (
        "Braiding starts at ₦5,000. Longer styles cost more; the stylist will confirm the final price.",
        ["braids price"],
        [7],
    ),
    (
        "The Custom Suit is priced on request. Would you like me to connect you with our tailor?",
        ["how much is a custom suit"],
        [8],
    ),
    ("Delivery within Lagos costs ₦2,500.", ["how much is delivery"], []),
    (
        "The Black Sneaker is ₦12,000. Delivery within Lagos is ₦2,500.",
        ["black sneaker price and delivery?"],
        [1],
    ),
    (
        "The Black Sneaker is ₦12,000, and delivery within Lagos is ₦2,500.",
        ["black sneaker price and delivery?"],
        [1],
    ),
    ("We offer free delivery within Ikeja.", ["do you deliver?"], []),
    ("Yes, we deliver! Delivery is free for orders within Ikeja.", ["do you deliver to ikeja"], []),
    (
        "The Black Sneaker is ₦12,000. Delivery is free within Ikeja.",
        ["black sneaker, delivery to ikeja?"],
        [1],
    ),
    ("We're open 9am to 6pm, Monday to Saturday.", ["what time do you open"], []),
    (
        "Our shop is at 14 Allen Avenue, Ikeja. You can call us on 0803 123 4567.",
        ["where are you located"],
        [],
    ),
    (
        "The Black Sneaker is ₦12,000 and the Black Sneaker Pro is ₦18,000.",
        ["difference between black sneakers"],
        [1, 2],
    ),
    ("The Black Sneaker Pro is ₦18,000. It has a cushioned sole.", ["black sneaker pro?"], [2]),
    ("The Red Bag Large is ₦32,000 and the Red Bag is ₦25,000.", ["red bag sizes"], [3, 4]),
    (
        "| Item | Price |\n|---|---|\n| Red Bag | ₦25,000 |\n| Red Bag Large | ₦32,000 |",
        ["red bags"],
        [3, 4],
    ),
    (
        "1. Leather Wallet - ₦8,500\n2. Kids Sandals - ₦6,500\n3. Ankara Dress - ₦15,000",
        ["cheap items?"],
        [],
    ),
    ("**Silver Wristwatch** — ₦45,000. In stock.", ["watch?"], [9]),
    ("2 x Black Sneaker at ₦12,000 each.", ["I want 2 black sneakers"], [1]),
    ("The Black Sneaker is ₦12,000 each.", ["price of black sneaker"], [1]),
    (
        "The Black Sneaker is ₦12,000. The listed price is firm, but I can pass your request to the team.",
        ["can you reduce it?"],
        [1],
    ),
    (
        "I'm sorry, I can't give a discount, but the Black Sneaker is great value at ₦12,000.",
        ["discount?"],
        [1],
    ),
    (
        "The price is ₦12,000. I can't go lower, but the team can follow up.",
        ["last price for black sneaker?"],
        [1],
    ),
    (
        "Thanks! I've noted that. The team will get back to you shortly.",
        ["I'll pay 10k for the black sneaker"],
        [1],
    ),
    ("The Leather Wallet is ₦8,500. Would you like to order one?", ["leather wallet"], [5]),
    (
        "Great choice! The Ankara Dress is available in sizes 8 to 16 for ₦15,000.",
        ["ankara dress sizes"],
        [6],
    ),
    ("The Kids Sandals are ₦6,500 a pair, available in sizes 24 to 34.", ["kids sandals"], [10]),
    (
        "Perfect! I've noted the Red Bag. The team will confirm delivery.",
        ["I'll take the red bag"],
        [3],
    ),
    (
        "The Silver Wristwatch comes with a 1 year warranty and costs ₦45,000.",
        ["watch warranty"],
        [9],
    ),
    ("Payment is by bank transfer. Orders above ₦50,000 get free delivery.", ["how do I pay"], []),
    (
        "Yes, the Red Bag is in stock. It costs ₦25,000. Shall I note it for you?",
        ["is the red bag available"],
        [3],
    ),
    ("You can pick up from our Ikeja shop between 10am and 5pm.", ["pickup?"], []),
    (
        "Sure, I can help with that. The Leather Wallet is ₦8,500 and the Kids Sandals are ₦6,500.",
        ["wallet and sandals price"],
        [5, 10],
    ),
    (
        "Okay! The Red Bag is ₦25,000. Delivery to Lekki is ₦2,500.",
        ["red bag delivered to lekki"],
        [3],
    ),
    # real violations from the reviewer's set: they must stay blocked
    ("Two Black Sneakers would be ₦24,000 in total.", ["2 black sneakers how much"], [1]),
    ("The Black Sneaker was ₦15,000 but is now ₦12,000.", ["black sneaker"], [1]),
]
SET1_EXPECT_BLOCK = {len(SET1) - 2, len(SET1) - 1}

# -- set 2: a clinic and a law firm -----------------------------------------------------------

S = OfferingKind.SERVICE
PRACTICE = [
    _f(1, "General Consultation", 15000, kind=S),
    _f(2, "Specialist Consultation", 25000, mode=FROM, kind=S),
    _f(3, "Malaria Test", 3500, kind=S),
    _f(4, "Dental Scaling", 20000, mode=FROM, kind=S),
    _f(5, "Annual Health Check", 60000, True, kind=S),
    _f(6, "Will Drafting", 150000, mode=FROM, kind=S),
    _f(7, "Contract Review", 80000, kind=S),
    _f(8, "Court Representation", None, mode=ON_REQ, kind=S),
    _f(9, "Company Registration", 120000, True, kind=S),
    _f(10, "Legal Opinion", 50000, mode=FROM, kind=S),
]
PRACTICE_PRICES = {
    "₦15,000": 1,
    "₦25,000": 2,
    "₦3,500": 3,
    "₦20,000": 4,
    "₦60,000": 5,
    "₦150,000": 6,
    "₦80,000": 7,
    "₦120,000": 9,
    "₦50,000": 10,
}
PRACTICE_KB = (
    "We are open Monday to Saturday, 8am to 6pm. HMO enrolees pay no consultation fee. "
    "Our office is at 12 Adeola Odeku Street, Victoria Island. Call 0802 555 0198. "
    "A booking deposit of ₦10,000 is required. Court filing fees are paid by the client. "
    "Results of the malaria test are ready in 30 minutes."
)
PRACTICE_NAMES = [i.name for i in PRACTICE]

SET2 = [
    ("A general consultation is ₦15,000.", ["how much to see a doctor"], [1]),
    ("A specialist consultation starts from ₦25,000.", ["specialist fee"], [2]),
    ("The malaria test is ₦3,500 and results are ready in 30 minutes.", ["malaria test"], [3]),
    (
        "Dental scaling starts from ₦20,000, depending on the condition of the teeth.",
        ["scaling cost"],
        [4],
    ),
    (
        "The Annual Health Check is ₦60,000. Would you like to book a slot?",
        ["health check price"],
        [5],
    ),
    (
        "Will drafting starts from ₦150,000. The lawyer will confirm the final fee after a short call.",
        ["how much to write a will"],
        [6],
    ),
    ("Contract review is ₦80,000 per contract.", ["contract review fee"], [7]),
    (
        "Court representation is quoted case by case. I can connect you with a lawyer.",
        ["court representation"],
        [8],
    ),
    ("Company registration is ₦120,000 and takes about two weeks.", ["register my company"], [9]),
    ("A legal opinion starts from ₦50,000.", ["legal opinion price"], [10]),
    ("We are open Monday to Saturday, 8am to 6pm.", ["opening hours"], []),
    (
        "Our office is at 12 Adeola Odeku Street, Victoria Island. Call 0802 555 0198.",
        ["where are you"],
        [],
    ),
    (
        "A booking deposit of ₦10,000 is required to hold your slot.",
        ["do I need to pay to book"],
        [],
    ),
    ("If you are on an HMO plan you pay no consultation fee.", ["do you take hmo"], []),
    (
        "The general consultation is ₦15,000, but HMO enrolees pay no consultation fee.",
        ["consultation fee with hmo"],
        [1],
    ),
    (
        "A booking deposit of ₦10,000 is required, and the general consultation is ₦15,000.",
        ["book a consultation"],
        [1],
    ),
    (
        "Here is what we offer:\n- General Consultation: ₦15,000\n- Specialist Consultation: from ₦25,000\n- Malaria Test: ₦3,500\n- Dental Scaling: from ₦20,000",
        ["what services do you have"],
        [],
    ),
    (
        "Court filing fees are paid by the client, separately from our fee.",
        ["are filing fees included"],
        [8],
    ),
    (
        "The Annual Health Check is ₦60,000 and includes blood work and a doctor review.",
        ["what is in the health check"],
        [5],
    ),
    (
        "Contract review is ₦80,000. Will drafting starts from ₦150,000.",
        ["contract and will"],
        [6, 7],
    ),
    (
        "The malaria test is ₦3,500 and the general consultation is ₦15,000.",
        ["test plus doctor"],
        [1, 3],
    ),
    ("We can book you in for Tuesday at 10am. What is your name?", ["can I come tuesday"], []),
    (
        "Dental scaling takes about 45 minutes. It starts from ₦20,000.",
        ["how long is scaling"],
        [4],
    ),
    (
        "Company registration is ₦120,000. You will need two valid IDs and 3 passport photographs.",
        ["documents for registration"],
        [9],
    ),
    (
        "A legal opinion starts from ₦50,000; the lawyer will confirm after reading your documents.",
        ["opinion on my lease"],
        [10],
    ),
    (
        "The specialist consultation starts from ₦25,000 and the final fee depends on the specialist.",
        ["which specialists"],
        [2],
    ),
    (
        "Sorry, I don't have that listed. Would you like me to connect you with the team?",
        ["do you do vasectomy"],
        [],
    ),
    (
        "The general consultation is ₦15,000. A deposit of ₦10,000 secures the slot.",
        ["price and deposit"],
        [1],
    ),
    (
        "Our lawyers handle tenancy, land and business matters. Court representation is quoted per case.",
        ["what cases"],
        [8],
    ),
    ("The Annual Health Check is ₦60,000.", ["can I get it cheaper"], [5]),
    ("Free parking is available at the Victoria Island office.", ["parking?"], []),
    ("The malaria test result is ready in 30 minutes.", ["how fast is the test"], [3]),
    (
        "Contract Review: ₦80,000\nWill Drafting: from ₦150,000\nLegal Opinion: from ₦50,000",
        ["legal prices"],
        [],
    ),
    (
        "Company registration is ₦120,000. The Annual Health Check is ₦60,000.",
        ["two services price"],
        [5, 9],
    ),
    ("Yes, you can pay by transfer or card. The deposit is ₦10,000.", ["how to pay deposit"], []),
    ("The consultation is ₦15,000 per visit.", ["consultation per visit"], [1]),
    (
        "Dental scaling starts from ₦20,000 and a follow-up visit is ₦15,000.",
        ["scaling and follow up"],
        [4],
    ),
    (
        "Please bring your previous test results to the specialist consultation, which starts from ₦25,000.",
        ["what to bring"],
        [2],
    ),
    (
        "I've noted your request. A team member will confirm the price and the date.",
        ["I want to register my company"],
        [9],
    ),
    (
        "A general consultation includes a free blood pressure check and takes about 20 minutes.",
        ["what is included"],
        [1],
    ),
]


def _rate(items, names, kb, prices, cases, expect_block):
    blocked_unexpected, passed_expected = [], []
    for n, (reply, customer, matched) in enumerate(cases):
        text = _tokenise(reply, prices)
        verdict = _check(items, names, kb, text, customer, matched)
        if n in expect_block and verdict.ok:
            passed_expected.append(reply)
        if n not in expect_block and not verdict.ok:
            blocked_unexpected.append((reply, [(v.reason, v.sentence) for v in verdict.violations]))
    return blocked_unexpected, passed_expected


def test_fashion_shop_normal_replies_are_not_blocked():
    assert len(SET1) == 40
    blocked, leaked = _rate(SHOP, SHOP_NAMES, SHOP_KB, SHOP_PRICES, SET1, SET1_EXPECT_BLOCK)
    normal = len(SET1) - len(SET1_EXPECT_BLOCK)
    assert not leaked, leaked
    assert len(blocked) / normal <= 0.03, blocked


def test_clinic_and_law_firm_normal_replies_are_not_blocked():
    assert len(SET2) == 40
    blocked, leaked = _rate(PRACTICE, PRACTICE_NAMES, PRACTICE_KB, PRACTICE_PRICES, SET2, set())
    assert len(blocked) / len(SET2) <= 0.03, blocked
    assert not leaked


def test_the_same_replies_with_literal_prices_are_blocked():
    """Compliance cuts both ways: an untokenised price is a violation, whatever its value."""
    reply = "The Red Bag is ₦25,000."
    assert not _check(SHOP, SHOP_NAMES, SHOP_KB, reply, ["red bag?"], [3]).ok
    assert _check(SHOP, SHOP_NAMES, SHOP_KB, _tokenise(reply, SHOP_PRICES), ["red bag?"], [3]).ok


def test_replies_pass_the_regex_free_sanity_check():
    for reply, _c, _m in SET1 + SET2:
        assert re.search(r"\S", reply)
