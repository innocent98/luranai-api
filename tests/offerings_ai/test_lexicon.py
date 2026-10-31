"""The price-promise lexicon: every phrase must be blocked, ordinary replies must not be."""

import pytest

from app.offerings.guard import check_reply
from tests.offerings_ai import lead8_sets as L
from tests.offerings_ai.test_domain_sets import grounding, item

EL = [
    item(1, "iPhone 14 Pro Max 256GB", 950000),
    item(2, "Samsung Galaxy S23", 720000),
    item(3, "PS5 Slim", 650000, neg=True),
    item(4, "AirPods Pro 2", 180000),
]
G = grounding(EL, [1])
QUIET = ["how much is the iphone 14 pro max", "hmm"]  # no bargaining wording, no figure

# thirty more promises (English, pidgin, and Yoruba / Hausa / Igbo from published usage; the last
# three groups have had no native-speaker review)
NEW_PROMISES = [
    "I can take something off for you.",
    "We'll trim the price a little.",
    "Let me shave some off.",
    "I'll bring it down for you.",
    "We can do a better rate today.",
    "I can give you a fair deal.",
    "Let me see what I can knock off.",
    "I'll cut it for you.",
    "We can lower the price if you take two.",
    "I'll give you the discounted price.",
    "I can make it more affordable for you.",
    "You'll pay less if you collect today.",
    "We can meet you halfway on the price.",
    "I'll work it out for you.",
    "Let me sort you out on the price.",
    "I'll do it cheap for you.",
    # pidgin
    "I go reduce am for you.",
    "We go drop am small for you.",
    "I fit give you better price.",
    "Make I dash you small on top the price.",
    "I go sort you out.",
    "No wahala, I go lower am.",
    # Yoruba
    "Mo maa din owo re ku.",
    "Mo le san owo re ku die.",
    # Hausa
    "Zan rage maka farashi.",
    "Za mu rangwame maka.",
    "Zan iya rage kudin.",
    # Igbo
    "Aga m ebelata ya maka gi.",
    "Anyi ga ebelata ọnụahịa ya.",
    "M ga-ebelata ya.",
]

# ordinary replies that share words with the lexicon but promise nothing
FINE = [
    "I can't give a discount, but the owner is open to offers; the team will follow up.",
    "We don't reduce prices in chat; a team member can discuss offers with you.",
    "I'm not able to lower the price myself.",
    "We can deliver today if you order before 2pm.",
    "I'll check the stock for you.",
    "Let me know your size and I'll note it.",
    "You'll get a confirmation from the team shortly.",
    "I can show you the other colours.",
    "We can arrange pickup at our Ikeja shop.",
    "I'll pass your request to the team.",
    "The cheaper-looking option is out of stock.",
    "Our cheapest accessory is the charger.",
    "We can do delivery on Saturday.",
    "I can send photos of the iPhone 14 Pro Max.",
    "The team can discuss pricing with you.",
]


@pytest.mark.parametrize("reply", L.REPLIES_DISCOUNT)
def test_the_fresh_figureless_promises_are_blocked_with_the_countdown_off(reply):
    assert not check_reply(reply, G, customer_texts=QUIET).ok, reply


@pytest.mark.parametrize("reply", L.REPLIES_DISCOUNT)
def test_the_fresh_figureless_promises_are_blocked_with_the_countdown_on(reply):
    assert not check_reply(reply, G, customer_texts=QUIET, offer_watch=True).ok, reply


@pytest.mark.parametrize("reply", NEW_PROMISES)
def test_more_promises_in_five_languages_are_blocked(reply):
    assert not check_reply(reply, G, customer_texts=QUIET).ok, reply


@pytest.mark.parametrize("reply", FINE)
def test_ordinary_replies_that_share_words_are_not_blocked(reply):
    v = check_reply(reply, G, customer_texts=QUIET)
    assert v.ok, [(x.reason, x.sentence) for x in v.violations]


def test_the_twenty_ordinary_replies_are_not_blocked():
    bad = []
    for reply in L.REPLIES_OK:
        v = check_reply(
            reply, G, customer_texts=["how much is the iphone 14 pro max", "ok"],
            kb_text="Delivery in Lagos is 3,000 naira.",
        )  # fmt: skip
        if not v.ok:
            bad.append((reply, [x.reason for x in v.violations]))
    assert len(bad) / len(L.REPLIES_OK) <= 0.03, bad


def test_agreement_words_count_once_the_customer_has_bargained_or_named_a_figure():
    for cust in (["abeg 900k"], ["can you reduce it"], ["how much", "I'll pay 800k", "ok"]):
        for reply in ("Okay, I'll accept that.", "Sure, that's a deal.", "Sounds good, agreed."):
            assert not check_reply(reply, G, customer_texts=cust).ok, (cust, reply)
    # no bargaining nearby: ordinary words stay ordinary
    assert check_reply("We accept bank transfer and card.", G, customer_texts=QUIET).ok
