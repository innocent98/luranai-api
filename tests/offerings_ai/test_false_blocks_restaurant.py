"""Block-rate regression, restaurant domain: 40 correctly tokenised replies (review pass 4).

A catalog with a 700-naira item switches on bare three-digit amounts, which is where "150 guests"
used to be read as money. At most 3% of normal replies may be blocked.
"""

from uuid import uuid4

from app.db.models.offering import OfferingAvailability, OfferingKind, OfferingPriceMode
from app.offerings.facts import OfferingFacts
from app.offerings.grounding import GroundingResult
from app.offerings.guard import check_reply

FIXED, FROM, ON_REQ = OfferingPriceMode.FIXED, OfferingPriceMode.FROM, OfferingPriceMode.ON_REQUEST


def _f(seq, name, price, negotiable=False, mode=FIXED, kind=OfferingKind.PRODUCT, note=None):
    return OfferingFacts(
        id=uuid4(), seq=seq, name=name, kind=kind, category=None, description="", price_mode=mode,
        price=price, price_note=note, availability=OfferingAvailability.IN_STOCK, quantity=None,
        negotiable=negotiable,
    )  # fmt: skip


ITEMS = [
    _f(1, "Jollof Rice", 3500), _f(2, "Fried Rice", 3800), _f(3, "Chicken Suya", 2500, True),
    _f(4, "Goat Pepper Soup", 4000), _f(5, "Small Chops Platter", 15000),
    _f(6, "Catering Package", 150000, False, FROM, OfferingKind.SERVICE, "per event"),
    _f(7, "Chapman", 1500), _f(8, "Zobo", 800), _f(9, "Moi Moi", 700), _f(10, "Asun", 4500),
    _f(11, "Party Jollof Cooler", 45000, note="serves 20"), _f(12, "Birthday Cake", None, False, ON_REQ),
]  # fmt: skip
NAMES = [i.name for i in ITEMS]
KB = (
    "Retrieved knowledge: We are open 8am to 10pm daily. Delivery within Yaba costs ₦1,000 and delivery to Lekki costs ₦2,000. "
    "Minimum order for delivery is ₦5,000. A 7.5% VAT applies to all orders. Call or WhatsApp 0809 555 1234. "
    "We are at 123 Herbert Macaulay Way, Yaba. Catering needs 72 hours notice. Free delivery on orders above ₦20,000."
)
CASES = [
    ("Our Jollof Rice is [[price:#1]] and the Fried Rice is [[price:#2]].", [1, 2]),
    ("Jollof Rice: [[price:#1]]\nFried Rice: [[price:#2]]\nChicken Suya: [[price:#3]]", []),
    ("Yes, the Goat Pepper Soup is available today for [[price:#4]].", [4]),
    ("The Small Chops Platter is [[price:#5]] and serves about 10 people.", [5]),
    ("Our Catering Package starts [[price:#6]], depending on the menu and guest count.", [6]),
    ("Catering needs 72 hours notice. The Catering Package is [[price:#6]].", [6]),
    (
        "For 150 guests, our Catering Package is [[price:#6]]; the team will confirm the final figure.",
        [6],
    ),
    ("Chapman is [[price:#7]] and Zobo is [[price:#8]].", [7, 8]),
    ("Moi Moi is [[price:#9]] per wrap.", [9]),
    ("Asun is [[price:#10]]. It's spicy!", [10]),
    ("The Party Jollof Cooler is [[price:#11]].", [11]),
    ("The Birthday Cake is priced on request; the team will confirm.", [12]),
    ("We are open 8am to 10pm daily.", []),
    ("Delivery within Yaba costs ₦1,000 and delivery to Lekki costs ₦2,000.", []),
    ("Delivery to Lekki costs ₦2,000.", []),
    ("The Jollof Rice is [[price:#1]]. Delivery to Lekki costs ₦2,000.", [1]),
    ("Jollof Rice is [[price:#1]], plus ₦1,000 delivery within Yaba.", [1]),
    ("Minimum order for delivery is ₦5,000.", []),
    ("A 7.5% VAT applies to all orders.", []),
    ("You can call or WhatsApp us on 0809 555 1234.", []),
    ("We are at 123 Herbert Macaulay Way, Yaba.", []),
    ("Free delivery on orders above ₦20,000.", []),
    ("Yes, we deliver to Lekki. Delivery there costs ₦2,000.", []),
    ("Sure! I've noted 2 plates of Jollof Rice. Can I have your name?", [1]),
    ("Got it: 2 Jollof Rice and 1 Chapman. What name should I put the order under?", [1, 7]),
    ("The Chicken Suya is [[price:#3]]. The owner is open to offers on it.", [3]),
    ("The Jollof Rice is [[price:#1]] per plate.", [1]),
    ("Zobo is [[price:#8]] a bottle; we also have Chapman at [[price:#7]].", [7, 8]),
    ("Our cheapest drink is Zobo at [[price:#8]].", [7, 8]),
    ("Sorry, we don't sell shawarma. Would you like to try our Chicken Suya at [[price:#3]]?", [3]),
    ("Each Moi Moi is [[price:#9]].", [9]),
    ("The Fried Rice comes with plantain and is [[price:#2]].", [2]),
    ("We're open from 8am. Orders placed before 9pm are delivered the same day.", []),
    ("Delivery usually takes 45 minutes within Yaba.", []),
    ("The Party Jollof Cooler is [[price:#11]] and needs 24 hours notice.", [11]),
    ("Yes, our Catering Package covers events of up to 500 guests.", [6]),
    ("The Goat Pepper Soup takes about 20 minutes to prepare.", [4]),
    ("Our kitchen is at 123 Herbert Macaulay Way; pickup is free.", []),
    ("Jollof Rice [[price:#1]] or Fried Rice [[price:#2]] — both come with chicken.", [1, 2]),
    ("Sure! The Small Chops Platter has 50 pieces and is [[price:#5]].", [5]),
]


def _blocked():
    bad = []
    for reply, matched in CASES:
        g = GroundingResult(
            mode="detail", block="", items=ITEMS, matched_seqs=list(matched), catalog_names=NAMES
        )
        v = check_reply(reply, g, customer_texts=["menu?"], kb_text=KB)
        if not v.ok:
            bad.append((reply, [x.reason for x in v.violations]))
    return bad


def test_the_restaurant_set_is_forty_replies():
    assert len(CASES) == 40


def test_restaurant_replies_are_not_blocked_beyond_the_target():
    bad = _blocked()
    assert len(bad) / len(CASES) <= 0.03, bad
