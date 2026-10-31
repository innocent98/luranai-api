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
    _item(10, "Canvas sneaker black pro", 18000),
    _item(11, "Red bag", 25000, negotiable=True),
    _item(12, "Red bag large", 32000),
]

CATALOG_NAMES = ["Silver wristwatch", "Ankara dress"]  # active items that are not in the block


def _grounding(items=ITEMS, *, strict=False, currency="NGN", matched=(), block=""):
    return GroundingResult(
        mode="unavailable" if strict else "detail",
        block=block,
        items=[] if strict else list(items),
        matched_seqs=list(matched),
        strict=strict,
        currency=currency,
        catalog_names=[] if strict else list(CATALOG_NAMES),
    )


def T(n: int) -> str:
    """The price token of item #n."""
    return f"[[price:#{n}]]"


@dataclass
class Case:
    reply: str
    ok: bool
    reason: str | None = None  # expected violation reason when not ok (None: any)
    customer: tuple = ()
    kb: str = ""
    tiers: tuple = ()
    strict: bool = False
    currency: str = "NGN"
    matched: tuple = ()
    watch: bool = False  # the offer countdown is running (an offer on an earlier turn)
    block: str = ""  # the injected block text (item descriptions the model may repeat)
    offer: bool = False  # the customer is bargaining / the model returned an offered_price


C = Case
BARGAIN = ("how much is the red bag", "I go pay 20k for the red bag")
CASES = [
    # --- accepted: prices only as tokens ------------------------------------------------------
    C(f"The black sneaker is {T(1)} and it is in stock.", True),
    C(f"Canvas sneaker black: {T(1)}\nCanvas sneaker white: {T(2)}", True),
    C(
        f"#1 Canvas sneaker black {T(1)}\n#2 Canvas sneaker white {T(2)}\n#3 Leather bag {T(3)}",
        True,
    ),
    C(f"Phone repair starts from {T(4)}.", True),
    C(f"The custom portrait is {T(6)}; I can connect you with the team.", True),
    C(f"The black sneaker is {T(1)}, cheaper than the white one at {T(2)}.", True),
    C(f"1. **Canvas sneaker black**\n - Price: {T(1)}", True),
    C(f"1. **Black sneaker**\n - Price: {T(1)}", False, "token_mismatch"),  # base or pro?
    C(f"| Item | Price |\n|---|---|\n| Black sneaker | {T(1)} |\n| Leather bag | {T(3)} |", True),
    C("Which size do you need, 41 or 42?", True),
    C("We have 2 pairs of the white sneaker in size 42.", True),
    C("Item #3 is the leather bag.", True),
    C("We opened in 2026 and reply 9:00 to 17:30.", True),
    C("Call us on 0803 123 4567 for the team.", True),
    C("We are open from 31/10/2026. Your reference is LUR-2041.", True),
    C(f"The Toyota Camry 2015 costs {T(8)}.", True),
    C(f"The N95 Mask is {T(9)}.", True),
    C("I've noted your request; the team will confirm.", True),
    C("Which item would you like?", True),
    C("Feel free to ask me anything about the red bag.", True),
    C(
        "The ankara dress is free size, 100% cotton and water resistant to 50m.",
        True,
        block="Ankara dress: free size, 100% cotton, water resistant to 50m",
    ),
    C("Visit us at Plot 1234, Admiralty Way. We have served over 2000 customers.", True),
    C("The red bag comes with a free dust bag.", True),
    C("I can't offer a discount on the black sneaker.", True),
    C("There is no discount on the black sneaker.", True),
    C(f"The black sneaker is {T(1)} and there is no discount.", True),
    C("Pickup is free from 10am to 5pm. You can drop off the red bag at our shop.", True),
    # knowledge base and plan amounts
    C("Delivery within Lagos is ₦2,000.", True, kb="Delivery in Lagos costs ₦2,000 per order."),
    C(
        f"The black sneaker is {T(1)} and delivery within Lagos is ₦2,000.",
        True,
        kb="Delivery in Lagos costs ₦2,000 per order.",
    ),
    C(
        "Orders above ₦50,000 get free delivery.",
        True,
        kb="Orders above ₦50,000 get free delivery.",
    ),
    C("Our Pro plan is ₦30,000 per month.", True, tiers=(30000,)),
    C("You offered ₦9,000.", True, customer=("I'll pay 9k",)),
    # --- literal amounts are never allowed, even correct ones ----------------------------------
    C("The black sneaker is ₦12,000.", False, "literal_amount"),
    C("The black sneaker is N12,000.", False, "literal_amount"),
    C("The black sneaker is 12,000 naira.", False, "literal_amount"),
    C("The black sneaker is 12k.", False, "literal_amount"),
    C("The black sneaker is ₦12000.", False, "literal_amount"),
    C("The black sneaker costs twelve thousand naira.", False, "literal_amount"),
    C("The black sneaker is 12000.", False, "literal_amount"),
    C("Canvas sneaker black: 12,000", False, "literal_amount"),
    C("The black sneaker is #9,000.", False, "literal_amount"),
    C("The black sneaker is #9000.", False, "literal_amount"),
    C("Black sneaker: #9k", False, "literal_amount"),
    C("Yes, #9,000 is fine for the black sneaker.", False),
    C("The black sneaker is ₦ 11 000.", False, "literal_amount"),
    C("The black sneaker is eleven thousand naira.", False, "literal_amount"),
    C("The black sneaker is NGN-11000.", False, "literal_amount"),
    C("The black sneaker is ₦11,000only.", False, "literal_amount"),
    C("Puff puff, 800 only.", False, "literal_amount"),
    C("The black sneaker is fifteen hundred naira.", False, "literal_amount"),
    C("The black sneaker is 5 grand.", False, "literal_amount"),
    C("The black sneaker is 12 thousand five hundred.", False, "literal_amount"),
    C("Two black sneakers come to ₦24,000.", False, "literal_amount"),
    C("2 x ₦12,000 = ₦24,000 for two black sneakers.", False, "literal_amount"),
    C("Your total is ₦57,000.", False, "literal_amount"),
    C("It's ₦15,000.", False, "literal_amount"),
    C("Black sneaker? Just ₦3,000!", False, "literal_amount"),
    C("The black sneaker was ₦15,000 but is now ₦12,000.", False, "literal_amount"),
    C("The black sneaker is $12,000.", False, "currency"),
    C("The black sneaker is 12,000 dollars.", False, "currency"),
    C("The black sneaker is ₦12,000.", False, "currency", currency="USD"),
    C("Delivery is ₦3,000.", False, "literal_amount"),
    C("Our warranty costs ₦1,750.", False, "literal_amount", kb="Delivery fee: ₦3,000."),
    C("Pro is ₦40,000 a month.", False, "literal_amount", tiers=(30000,)),
    C("The black sneaker is ₦30,000 on our Pro plan.", False, "literal_amount", tiers=(30000,)),
    C("It's ₦9,000 as you mentioned.", False, "literal_amount", customer=("I'll pay 9k",)),
    C("Your budget of ₦9,000 covers it.", False, "literal_amount", customer=("I'll pay 9k",)),
    C(
        "₦9,000 as you said. Send your address for delivery.",
        False,
        "literal_amount",
        customer=("I'll pay 9k",),
    ),
    C("The leather bag is ₦50,000 as you said.", False, "literal_amount", customer=("50k",)),
    C("The Silver wristwatch costs ₦40,000.", False, "literal_amount"),
    C(
        "Silver wristwatch now costs ₦40,000.",
        False,
        "literal_amount",
        kb="Silver wristwatch now costs ₦40,000.",
    ),
    C(
        "The red bag sells for ₦20,000.",
        False,
        "literal_amount",
        kb="The red bag sells for ₦20,000.",
    ),
    C("Okay, I can do ₦9,000 for you.", False, "literal_amount", kb="Black sneaker ₦9,000"),
    C("You can get 2 black sneakers for ₦12,000.", False, "literal_amount"),
    C("Delivery is 2,5k.", False, "literal_amount"),
    C("Delivery is 2,5k.", True, kb="Delivery costs ₦2,500."),
    # --- tokens: unknown, foreign, malformed, on the wrong item -------------------------------
    C("The black sneaker is [[price:#99]].", False, "unknown_token"),
    C("The silver wristwatch is [[price:#40]].", False, "unknown_token"),
    C("The black sneaker is [[price: abc]].", False, "bad_token"),
    C("The black sneaker is [[price:#1.", False, "bad_token"),
    C("The black sneaker is [[free]].", False, "bad_token"),
    C(f"The leather bag is {T(1)}.", False, "token_mismatch"),
    C(f"{T(1)} gets you the Canvas sneaker black pro.", False, "token_mismatch"),
    C(f"The Canvas sneaker black pro is {T(10)}.", True),
    C(f"The Canvas sneaker black is {T(1)} and the Canvas sneaker black pro is {T(10)}.", True),
    C(
        f"The Canvas sneaker black pro and the Canvas sneaker black cost {T(1)} and {T(10)}.",
        False,
        "token_mismatch",
    ),  # respectively: the pro one would get the base price
    C(  # a list of names then a list of tokens cannot be bound: write each price beside its item
        f"The Canvas sneaker black pro and the Canvas sneaker black cost {T(10)} and {T(1)}.",
        False,
        "token_mismatch",
    ),
    C(f"The Red bag large is {T(11)}.", False, "token_mismatch"),
    C(f"The Red bag large is {T(12)}.", True),
    C(f"#12 Red bag large - {T(12)}", True),
    C(f"#11 Red bag - {T(11)}\n#12 Red bag large - {T(12)}", True),
    # --- bundles and "same price" claims, which a token alone would let through ----------------
    C(f"Get 2 for {T(1)}!", False, "bundle_price"),
    C(f"2 pairs of black sneakers for {T(1)}", False, "bundle_price"),
    C(f"Buy 2 black sneakers, pay {T(1)}.", False, "bundle_price"),
    C(f"2 black sneakers at {T(1)} each.", True),
    C(f"The black sneaker is {T(1)} per pair.", True),
    C(
        f"The black sneaker is {T(1)}. The pro version is the same price.",
        False,
        "same_price_claim",
    ),
    C(f"The red bag is {T(11)} and the large one costs the same.", False, "same_price_claim"),
    C(f"Both the black sneaker and the pro are {T(1)}.", False),
    # --- discount, freebie and percentage wording ---------------------------------------------
    C("Black sneaker is half price today.", False, "discount_wording"),
    C("You can have the black sneaker for free.", False, "freebie_wording"),
    C("Buy one black sneaker, get one free.", False, "discount_wording"),
    C(
        "Take the black sneaker and add the leather bag at no extra charge.",
        False,
        "discount_wording",
    ),
    C("The black sneaker is yours at no cost.", False, "discount_wording"),
    C("The black sneaker comes with delivery on us.", False, "discount_wording"),
    C("I can offer 10% off the black sneaker.", False, "discount_wording"),
    C("Sure, 10 percent off then.", False, "discount_wording"),
    C("I'll take 500 off the black sneaker.", False),
    C("I'll take 500 off.", False),
    C("I can give a discount on the black sneaker.", False, "discount_wording"),
    C("No discount apart from half price on the black sneaker.", False, "discount_wording"),
    C("There's no discount other than half price on the black sneaker.", False, "discount_wording"),
    C("You won't believe this discount on the black sneaker.", False, "discount_wording"),
    C("Don't miss this discount on the black sneaker.", False, "discount_wording"),
    C("Don't miss our half price deal on the black sneaker.", False, "discount_wording"),
    C("We won't refuse a discount on the black sneaker.", False, "discount_wording"),
    C("We never say no to discount on the black sneaker.", False, "discount_wording"),
    C("I can't deny you a discount on the black sneaker.", False, "discount_wording"),
    C("Why not give you the black sneaker for free?", False, "freebie_wording"),
    C("No kidding \u2014 half price on the black sneaker.", False, "discount_wording"),
    # --- automated review of e71cdd6 -------------------------------------------------------------
    C(f"The silver wristwatch is {T(1)}.", False, "token_mismatch"),
    C(f"The Ankara dress is {T(3)}.", False, "token_mismatch"),
    C(f"The leather bag is great. It costs {T(1)}.", False, "token_mismatch"),
    C(f"The leather bag is great. It costs {T(3)}.", True),
    C(f"#1 Canvas sneaker black - {T(1)}, #2 Canvas sneaker white - {T(2)}.", True),
    C("The wristwatch is half price today.", False, "discount_wording"),
    C("Our dress is on discount this week.", False, "discount_wording"),
    C("Canvas sneaker black 12000", False, "literal_amount"),
    C("Black sneaker model 5100", False, "literal_amount"),
    C("Visit Plot 1234, Admiralty Way. We have served over 2000 customers.", True),
    C(
        "You offered ₦9,000. Okay, deal!",
        False,
        "agreement_after_offer",
        customer=("ok thanks", "I'll pay 9k"),
        matched=(1,),
    ),
    C(
        "You offered ₦9,000. We can do that.",
        False,
        "agreement_after_offer",
        customer=("I'll pay 9k", "yes please"),
        matched=(1,),
    ),
    C(
        f"The black sneaker is {T(1)}. Perfect.",
        False,
        "agreement_after_offer",
        customer=("I'll pay 9k", "ok"),
        matched=(1,),
    ),
    # --- strict mode (lookup failed) ------------------------------------------------------------
    C("I cannot check the list right now. Shall I connect you with the team?", True, strict=True),
    C("The black sneaker is ₦12,000.", False, "strict", strict=True),
    C(f"The black sneaker is {T(1)}.", False, "strict", strict=True),
    C("Delivery is ₦2,000.", False, "strict", strict=True, kb="Delivery is ₦2,000."),
    C("You said ₦20,000.", False, "strict", strict=True, customer=("₦20,000",)),
]

# --- agreement wording while the customer bargains (the backstop behind code-only replies) -------
_AGREEMENTS = [
    "Okay, half price works.",
    "Sure, I can reduce it for you.",
    "Deal!",
    "You asked for 20%. Fine.",
    "We'll honour that.",
    "That's doable!",
    "I'll put it through at your price.",
    "Let's proceed with the red bag at that price.",
    "We fit do am.",
    "Done deal!",
    "No wahala at all.",
    "Perfect.",
    "That should be possible.",
    "Consider it settled.",
    "Alright, I've updated your order.",
    "Okay.",
    "We can do that.",
    "That's fair, let's go with that.",
]
CASES += [
    C(text, False, "agreement_after_offer", customer=BARGAIN, offer=True, matched=(11,))
    for text in _AGREEMENTS
]
CASES += [
    C(
        "I can't accept that price, but the owner is open to offers.",
        True,
        customer=BARGAIN,
        offer=True,
    ),
    C("The owner is open to offers; the team will follow up.", True, customer=BARGAIN, offer=True),
    C(
        f"The red bag is listed at {T(11)}; the team will review your request.",
        True,
        customer=BARGAIN,
        offer=True,
        matched=(11,),
    ),
    C("Okay, great choice! Which size?", True),
    C(f"The red bag is {T(11)}. Sure thing, it's in stock.", True),
    C(f"Alright, the price is {T(11)}.", True, matched=(11,)),
    C(f"Our best price on it is {T(11)}.", True, matched=(11,)),
    C("The black sneaker is £12.", False, "currency"),
    C("Item #1 and item #2 are both in stock.", True),
]


# --- review pass 4: KB amounts, token binding, totals and freebies --------------------------------
CASES += [
    # promotions, discounts and terms the tenant never stated (review pass 4, the lead's strings)
    C(f"The Canvas sneaker black is {T(1)}. Buy two and the second is half off.", False),
    C(f"The Canvas sneaker black is {T(1)}. A deposit of 50% secures it.", False, "percent_claim"),
    C(f"The Canvas sneaker black is {T(1)}, promo price this week.", False, "freebie_wording"),
    C(f"The Canvas sneaker black is on sale at {T(1)}.", False, "freebie_wording"),
    C(f"The Canvas sneaker black is {T(1)}, minus our loyalty bonus.", False, "freebie_wording"),
    C(f"The Canvas sneaker black is {T(1)}, and the price includes delivery anywhere.", False),
    C(f"The Canvas sneaker black is {T(1)}. The Red bag? Same.", False, "same_price_claim"),
    C(f"The Canvas sneaker black is {T(1)}. The Red bag matches it.", False),
    C(f"Everything in the shop is {T(1)}.", False, "bundle_price"),
    C(f"Canvas sneaker black x2 - {T(1)}", False, "bundle_price"),
    C(f"Buy the Canvas sneaker black at {T(1)} and get the Leather bag too.", False),
    C(f"The Canvas sneaker black is {T(1)}, and the Leather bag comes with it.", False),
    C(f"The Red bag is {T(11)}.{T(12)}", False, "token_mismatch"),
    C("A 7.5% VAT applies to all orders.", True, kb="A 7.5% VAT applies to all orders."),
    C("A 7.5% VAT applies to all orders.", False, "percent_claim"),
]

CASES += [
    # review pass 5: carried binding names only the most specific item, never two candidates
    C(f"The Canvas sneaker black is a classic.\nIt costs {T(10)}.", False, "token_mismatch"),
    C(f"The Canvas sneaker black is a classic.\nIt costs {T(1)}.", True),
    C(f"*Canvas sneaker black*\nPrice: {T(10)}", False, "token_mismatch"),
    C(f"The sneaker is a classic.\nIt costs {T(1)}.", False, "token_mismatch"),
    # concessions with no offer in play, and wording the old patterns missed
    C("For you, I can go a little lower on the Red bag.", False, "discount_wording"),
    C("Sure, the Red bag can be yours for less.", False, "discount_wording"),
    C(f"The least we can do is a little below {T(11)}.", False, "discount_wording"),
    C(f"I can manage something on the Red bag, {T(11)} is not final.", False, "discount_wording"),
    # ordinary replies during an offer countdown: only sentences about an item or price are judged
    C(
        "Yes, we deliver to Lekki within 2 days.",
        True,
        customer=("abeg 9000", "ok", "do you deliver to Lekki?"),
        offer=False,
    ),
    C(
        "Sure, we are open from 9am to 5pm.",
        True,
        customer=("abeg 9000", "ok", "when do you open?"),
    ),
    C(
        "Of course, we accept bank transfer.",
        True,
        customer=("abeg 9000", "ok", "can I pay by transfer?"),
    ),
    C(
        "Okay, the team can book Saturday.",
        True,
        customer=("abeg 9000", "ok", "can I pick up Saturday?"),
    ),
    C("Yes, we deliver to Lekki within 2 days.", True, customer=("my order number is 10021",)),
    C("Yes, we deliver.", True, customer=("I paid 5000 deposit yesterday",)),
    C(
        "Deal! I'll let the team know.",
        False,
        customer=("how much is the red bag", "I fit pay 18k"),
    ),
    C(
        "Sure, we can do that for you!",
        False,
        customer=("how much is the red bag", "Pls sell for 18,000"),
    ),
    # spec shorthand and descriptive numbers are not money, bundles or percent claims
    C("The Samsung TV is 4K UHD and supports 4K.", True),
    C("It charges to 50% in 30 minutes.", True),
    C("The phone is 100% original with battery health 90%.", True),
    C(f"The Canvas sneaker black is {T(1)} and comes with 2 SIM slots.", True),
    C(f"The Canvas sneaker black is {T(1)} with a 14 inch screen.", True),
    C(f"The Canvas sneaker black is {T(1)}; parking for 2 cars, 5 toilets.", True),
    C(f"The Canvas sneaker black is {T(1)}; it takes 5 to 6 hours.", True),
    C(
        f"The Canvas sneaker black is {T(1)} and a deposit of 50% secures it.",
        False,
        "percent_claim",
    ),
    C(f"The Canvas sneaker black is {T(1)} with a 20% discount.", False),
]


CASES += [
    C(
        "Sure! The Red bag comes in red and wine.",
        True,
        customer=("abeg 9000", "ok", "where is your shop?"),
        watch=True,
    ),
    C(
        "Yes, we deliver to Lekki within 2 days.",
        True,
        customer=("abeg 9000", "ok", "where is your shop?"),
        watch=True,
    ),
    C(
        "Of course, you can pick it up at our Ikeja shop.",
        True,
        customer=("abeg 9000", "ok", "where is your shop?"),
        watch=True,
    ),
    C(
        "Okay, the team will reach you shortly.",
        True,
        customer=("abeg 9000", "ok", "where is your shop?"),
        watch=True,
    ),
    C(
        "Fine, I've noted your address.",
        True,
        customer=("abeg 9000", "ok", "where is your shop?"),
        watch=True,
    ),
    C(
        "Yes, it's original leather.",
        True,
        customer=("abeg 9000", "ok", "where is your shop?"),
        watch=True,
    ),
    C(
        "No problem, I'll let the team know.",
        True,
        customer=("abeg 9000", "ok", "where is your shop?"),
        watch=True,
    ),
    C(
        "Delivery is possible on Saturday.",
        True,
        customer=("abeg 9000", "ok", "where is your shop?"),
        watch=True,
    ),
    C(
        "Done! Your details are saved.",
        True,
        customer=("abeg 9000", "ok", "where is your shop?"),
        watch=True,
    ),
    C(
        "Absolutely, we accept bank transfer.",
        True,
        customer=("abeg 9000", "ok", "where is your shop?"),
        watch=True,
    ),
    C(
        "We can do delivery to Ajah on Friday.",
        True,
        customer=("abeg 9000", "ok", "where is your shop?"),
        watch=True,
    ),
    C(
        "Great news, it's in stock.",
        True,
        customer=("abeg 9000", "ok", "where is your shop?"),
        watch=True,
    ),
    C(
        "Alright, anything else I can help with?",
        True,
        customer=("abeg 9000", "ok", "where is your shop?"),
        watch=True,
    ),
    C(
        "Perfect, thanks for your patience.",
        True,
        customer=("abeg 9000", "ok", "where is your shop?"),
        watch=True,
    ),
    C(
        "That works, the team will call you.",
        True,
        customer=("abeg 9000", "ok", "where is your shop?"),
        watch=True,
    ),
    # during the countdown a sentence about an item, a price or an offer is still judged
    C(
        "Okay, deal on the Red bag.",
        False,
        "agreement_after_offer",
        customer=("abeg 9000", "ok", "where is your shop?"),
        watch=True,
    ),
    C(
        "Yes, we agree to your price.",
        False,
        "agreement_after_offer",
        customer=("abeg 9000", "ok", "where is your shop?"),
        watch=True,
    ),
    C(
        "Yes, we accept your offer.",
        False,
        "agreement_after_offer",
        customer=("abeg 9000", "ok", "where is your shop?"),
        watch=True,
    ),
    # an asking turn after the countdown ended is itself bargaining wording
    C(
        "Yes, we agree.",
        False,
        "agreement_after_offer",
        customer=(
            "how much is the red bag",
            "abeg 18k",
            "ok",
            "when can you deliver",
            "so you agree to my price?",
        ),
        matched=(11,),
    ),
    C(
        "Since you're a returning customer, the team will sort you out with something nice on the Red bag.",
        False,
        "discount_wording",
    ),
]


CASES += [
    # a sold-out item's token next to "available" is a claim the shop cannot honour
    C(f"The Sold out sandals are {T(5)} and available now.", False, "sold_out_claim"),
    C(f"The Sold out sandals are {T(5)}, but they are sold out right now.", True),
]


CASES += [
    C(f"The Canvas sneaker black is {T(1)} and it is 100% original.", True),
    C(f"The Canvas sneaker black is {T(1)} with 2 parking spaces.", True),
    C(f"The Canvas sneaker black is {T(1)} with a 20% discount.", False),
]

BARE = ("how much is the red bag", "abeg 9000")
CASES += [
    # an offer written as a bare number still puts the reply under the agreement rules
    C("Okay, deal! I've noted it.", False, "agreement_after_offer", customer=BARE, matched=(11,)),
    C("Done! I'll get it ready.", False, "agreement_after_offer", customer=BARE, matched=(11,)),
    C("Alright, that works. Send your name and email.", False, customer=BARE, matched=(11,)),
    C(f"The red bag is {T(11)}.", True, customer=BARE, matched=(11,)),
    C("Okay, deal!", False, customer=("how much is the red bag", "make it 9,000"), matched=(11,)),
    C(
        "Sure, no problem.",
        True,
        customer=("how much is the red bag", "can you do size 44?"),
        matched=(11,),
    ),
]

KBD = "Delivery outside Lagos is ₦4,000. Minimum order is ₦10,000."
CASES += [
    # a KB amount is allowed only in a clause about the same thing, never as an item's price
    C("The Canvas sneaker black is ₦4,000.", False, "literal_amount", kb=KBD),
    C("Pay in 3 installments of ₦4,000.", False, "literal_amount", kb=KBD),
    C(
        "Okay, you can have the Canvas sneaker black for ₦10,000.",
        False,
        customer=("make it 10,000",),
        kb=KBD,
        offer=True,
    ),
    C("Okay, you can have the Canvas sneaker black for ₦10,000.", False, kb=KBD),
    C(
        "Okay, you can have it for ₦10,000.",
        False,
        customer=("make it 10,000",),
        kb=KBD,
        offer=True,
    ),
    C("Delivery outside Lagos is ₦4,000.", True, kb=KBD),
    C(f"The Canvas sneaker black is {T(1)} and delivery outside Lagos is ₦4,000.", True, kb=KBD),
    # a token belongs to the item written beside it, not just to some item of the turn
    C(f"It's {T(1)}.", False, "token_mismatch", matched=(11,)),
    C(
        f"Yes, it's {T(1)}.",
        False,
        "token_mismatch",
        customer=("the red bag is [[price:#1]] abi?",),
        matched=(11,),
    ),
    C(f"Red bag - {T(1)}, Canvas sneaker black - {T(11)}", False, "token_mismatch"),
    C(f"Red bag - {T(11)}, Canvas sneaker black - {T(1)}", True),
    C(f"It's {T(11)}.", True, matched=(11,)),
    C(f"It's {T(11)}.", False, "token_mismatch"),
    C(f"{T(1)} for the Canvas sneaker black and {T(11)} for the Red bag.", True),
    C(f"The pro version of the Canvas sneaker black is {T(1)}.", False, "token_mismatch"),
    C(f"The pro version of the Canvas sneaker black is {T(10)}.", True),
    C(f"The Red bag in large is {T(11)}.", False, "token_mismatch"),
    # totals and freebies written around a correct token
    C(f"Two Canvas sneaker black would be {T(1)}.", False, "bundle_price"),
    C(f"Your total for 2 pairs is {T(1)}.", False, "bundle_price"),
    C(f"With the Red bag the pair comes to {T(11)}.", False, "bundle_price"),
    C(f"The Canvas sneaker black is {T(1)}; get the Red bag for the same.", False),
    C(f"All our bags are {T(3)}.", False, "bundle_price"),
    C(f"The second is half off, {T(1)}.", False),
    C(
        f"The Canvas sneaker black is {T(1)} and comes with a free Leather bag.",
        False,
        "freebie_wording",
    ),
    C(
        f"The Canvas sneaker black is {T(1)}, complimentary Leather bag included.",
        False,
        "freebie_wording",
    ),
    C(f"Get the Leather bag as a gift with the Canvas sneaker black at {T(1)}.", False),
    C(
        f"The Canvas sneaker black is cheaper than the Silver wristwatch at {T(1)}.",
        False,
        "token_mismatch",
    ),
    C("Canvas sneaker black - ₦4,000", False, "literal_amount", kb=KBD),
    C(f"{T(1)} for something called the Red bag", False, "token_mismatch"),
    C(f"{T(1)} for the cheap Red bag", False, "token_mismatch"),
    C(f"The Canvas sneaker black is {T(1)} each.", True),
    C(f"The Canvas sneaker black comes in 2 colours at {T(1)}.", True),
    C(f"The Canvas sneaker black is {T(1)} and comes with free delivery.", True),
]


@pytest.mark.parametrize(
    "case", CASES, ids=[f"{i:03d}-{c.reply[:48]}" for i, c in enumerate(CASES)]
)
def test_guard_verdict(case: Case):
    verdict = check_reply(
        case.reply,
        _grounding(
            strict=case.strict, currency=case.currency, matched=case.matched, block=case.block
        ),
        customer_texts=case.customer,
        kb_text=case.kb,
        tier_amounts=case.tiers,
        offer_in_play=case.offer,
        offer_watch=case.watch,
    )
    assert verdict.ok is case.ok, [(v.reason, v.sentence) for v in verdict.violations]
    if not case.ok and case.reason is not None:
        assert case.reason in {v.reason for v in verdict.violations}, [
            (v.reason, v.sentence) for v in verdict.violations
        ]


def test_at_least_one_hundred_and_fifty_reply_strings():
    assert len(CASES) >= 150


def test_violation_reports_the_sentence_and_amount():
    v = check_reply("Hello. The black sneaker is ₦11,000. Bye.", _grounding())
    assert not v.ok
    assert v.violations[0].sentence == "The black sneaker is ₦11,000."
    assert v.violations[0].amounts == [11000]


def test_empty_reply_is_ok():
    assert check_reply("", _grounding()).ok


def test_amount_inside_decimal_is_not_split_into_sentences():
    v = check_reply(
        "Delivery is ₦2,000.50. Anything else?", _grounding(), kb_text="Delivery ₦2,000.50."
    )
    assert v.ok


def test_a_catalog_name_outside_the_block_cannot_launder_a_kb_price():
    g = _grounding(items=[ITEMS[0]], matched=(1,))
    kb = "Silver wristwatch now costs ₦40,000. Delivery is ₦2,000."
    assert not check_reply("The watch costs ₦40,000.", g, kb_text=kb).ok
    assert check_reply("Delivery is ₦2,000.", g, kb_text=kb).ok
