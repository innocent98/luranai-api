"""Block-rate regression, electronics / salon / real estate (review pass 5): 40 correct replies.

Spec shorthand (4K, GB, mAh, inch), descriptive counts ("2 SIM slots", "parking for 2 cars") and
property percentages ("charges to 50%") are not money, bundle prices or percent claims. At most 3%
of normal replies may be blocked.
"""

from uuid import uuid4

from app.db.models.offering import OfferingAvailability, OfferingKind, OfferingPriceMode
from app.offerings.facts import OfferingFacts
from app.offerings.grounding import GroundingResult
from app.offerings.guard import check_reply

K = OfferingKind
FIXED, FROM, ON_REQ = OfferingPriceMode.FIXED, OfferingPriceMode.FROM, OfferingPriceMode.ON_REQUEST


def F(seq, name, price, mode=FIXED, kind=K.PRODUCT, note=None, desc=""):
    return OfferingFacts(
        id=uuid4(), seq=seq, name=name, kind=kind, category=None, description=desc, price_mode=mode,
        price=price, price_note=note, availability=OfferingAvailability.IN_STOCK, quantity=None,
        negotiable=False,
    )  # fmt: skip


E = [
    F(1, "Tecno Camon 30", 285000, desc="8GB RAM, 256GB, 5000mAh, 6.7 inch, 120Hz"),
    F(2, "Samsung Galaxy A15", 210000), F(3, "iPhone 13", 650000), F(4, "Oraimo FreePods 4", 28000),
    F(5, "HP EliteBook 840 G5", 420000), F(6, "Anker 20W Charger", 15000),
    F(7, "Samsung 55 inch Smart TV", 780000),
]  # fmt: skip
S = [
    F(11, "Knotless Braids", 25000, FROM, K.SERVICE), F(12, "Silk Press", 18000, kind=K.SERVICE),
    F(13, "Gel Manicure", 8000, kind=K.SERVICE), F(14, "Lash Extensions", 15000, FROM, K.SERVICE),
    F(15, "Bridal Makeup", None, ON_REQ, K.SERVICE),
]  # fmt: skip
P = [
    F(21, "3 Bedroom Flat in Lekki Phase 1", 4500000, note="per annum"),
    F(22, "4 Bedroom Terrace Duplex, Ikota", 85000000),
    F(23, "500sqm Land in Ibeju-Lekki", 12000000),
    F(24, "2 Bedroom Apartment, Yaba", 2800000, note="per annum"),
]  # fmt: skip
KBE = (
    "Delivery within Lagos is ₦3,000. All phones come with 1 year warranty. Laptops have 2 years "
    "warranty. Store hours 9am to 7pm. Call 0809 555 1234."
)
KBS = "Home service attracts an extra ₦5,000 within Lekki. Deposit of ₦5,000 secures your booking. We open 8am to 8pm."
KBP = (
    "Agency fee is 10% and legal fee is 10% of the annual rent. Caution deposit is one year. "
    "Inspection fee ₦10,000. Call 0803 222 3344."
)
CASES = [
    (
        E,
        KBE,
        [1],
        ["how much is the camon 30?"],
        "The Tecno Camon 30 is [[price:#1]]. It has 8GB RAM, 256GB storage, a 5000mAh battery and a 6.7 inch 120Hz display.",
    ),
    (
        E,
        KBE,
        [1],
        ["specs?"],
        "Tecno Camon 30: 8GB RAM, 256GB, 5000mAh, 6.7 inch, 120Hz. Price: [[price:#1]].",
    ),
    (
        E,
        KBE,
        [1],
        ["warranty?"],
        "The Tecno Camon 30 comes with 1 year warranty. It costs [[price:#1]].",
    ),
    (
        E,
        KBE,
        [2, 1],
        ["samsung vs tecno"],
        "The Samsung Galaxy A15 is [[price:#2]] and the Tecno Camon 30 is [[price:#1]]. The Camon has 8GB RAM and 256GB storage.",
    ),
    (
        E,
        KBE,
        [3],
        ["iphone 13 price"],
        "The iPhone 13 is [[price:#3]] with 1 year warranty. Delivery within Lagos is ₦3,000.",
    ),
    (
        E,
        KBE,
        [5],
        ["laptop"],
        "The HP EliteBook 840 G5 is [[price:#5]]. It comes with 2 years warranty, 16GB RAM and a 512GB SSD.",
    ),
    (
        E,
        KBE,
        [4],
        ["earbuds"],
        "Oraimo FreePods 4 - [[price:#4]]. Up to 35 hours battery life with the case.",
    ),
    (
        E,
        KBE,
        [6],
        ["charger"],
        "The Anker 20W Charger is [[price:#6]]. It charges an iPhone 13 to 50% in about 30 minutes.",
    ),
    (
        E,
        KBE,
        [7],
        ["55 inch tv"],
        "Our Samsung 55 inch Smart TV is [[price:#7]]. It's 4K UHD with 3 HDMI ports.",
    ),
    (
        E,
        KBE,
        [1, 6],
        ["phone and charger"],
        "Tecno Camon 30: [[price:#1]]\nAnker 20W Charger: [[price:#6]]\nI can note both for the team.",
    ),
    (
        E,
        KBE,
        [1],
        ["do you have it in stock"],
        "Yes, the Tecno Camon 30 is in stock at [[price:#1]]. You can call us on 0809 555 1234.",
    ),
    (
        E,
        KBE,
        [2],
        ["samsung a15"],
        "The **Samsung Galaxy A15** is **[[price:#2]]**. It has a 6.5 inch screen and 128GB storage.",
    ),
    (E, KBE, [3], ["iphone"], "- iPhone 13: [[price:#3]] (128GB)\n- 1 year warranty"),
    (
        E,
        KBE,
        [1],
        ["battery?"],
        "The Tecno Camon 30 has a 5000mAh battery that lasts about 2 days. Price is [[price:#1]].",
    ),
    (
        E,
        KBE,
        [1, 2, 3],
        ["phones"],
        "Here are our phones:\n1. Tecno Camon 30 - [[price:#1]]\n2. Samsung Galaxy A15 - [[price:#2]]\n3. iPhone 13 - [[price:#3]]",
    ),
    (
        S,
        KBS,
        [11],
        ["braids price"],
        "Knotless Braids start from [[price:#11]] and take about 5 to 6 hours.",
    ),
    (S, KBS, [12], ["silk press"], "A Silk Press is [[price:#12]] and takes about 2 hours."),
    (S, KBS, [13], ["nails"], "Gel Manicure is [[price:#13]]. It lasts 2 to 3 weeks."),
    (
        S,
        KBS,
        [14],
        ["lashes"],
        "Lash Extensions start from [[price:#14]]. A classic set takes 90 minutes; refills are needed every 2 to 3 weeks.",
    ),
    (
        S,
        KBS,
        [15],
        ["bridal"],
        "Bridal Makeup is priced on request. Would you like me to connect you with our makeup artist?",
    ),
    (
        S,
        KBS,
        [11],
        ["home service?"],
        "Yes, Knotless Braids from [[price:#11]]. Home service attracts an extra ₦5,000 within Lekki.",
    ),
    (
        S,
        KBS,
        [11],
        ["how do i book"],
        "To book Knotless Braids ([[price:#11]]), a deposit of ₦5,000 secures your slot.",
    ),
    (
        S,
        KBS,
        [12, 13],
        ["silk press and nails"],
        "Silk Press is [[price:#12]] (2 hours) and Gel Manicure is [[price:#13]] (45 minutes).",
    ),
    (
        S,
        KBS,
        [11],
        ["what time"],
        "We open 8am to 8pm. Knotless Braids from [[price:#11]] take 5 to 6 hours, so come in by 2pm.",
    ),
    (
        S,
        KBS,
        [14],
        ["lashes"],
        "Lash Extensions from [[price:#14]]. Hybrid and volume sets take 2 to 3 hours.",
    ),
    (
        S,
        KBS,
        [13],
        ["nails for 2"],
        "Gel Manicure is [[price:#13]] per person, and we can take 2 of you at 3pm.",
    ),
    (
        S,
        KBS,
        [11],
        ["braids length"],
        "Knotless Braids start from [[price:#11]] for shoulder length; waist length takes 8 hours.",
    ),
    (
        P,
        KBP,
        [21],
        ["lekki flat"],
        "The 3 Bedroom Flat in Lekki Phase 1 is [[price:#21]]. It has 3 bathrooms, a guest toilet and parking for 2 cars.",
    ),
    (
        P,
        KBP,
        [22],
        ["duplex"],
        "The 4 Bedroom Terrace Duplex, Ikota is [[price:#22]]. It sits on 300sqm with a BQ.",
    ),
    (P, KBP, [23], ["land"], "The 500sqm Land in Ibeju-Lekki is [[price:#23]] with C of O."),
    (
        P,
        KBP,
        [24],
        ["yaba"],
        "The 2 Bedroom Apartment, Yaba is [[price:#24]]. It's on the 2nd floor, 5 minutes from Unilag.",
    ),
    (
        P,
        KBP,
        [21],
        ["fees"],
        "The 3 Bedroom Flat in Lekki Phase 1 is [[price:#21]]. Agency fee is 10% and legal fee is 10% of the annual rent.",
    ),
    (
        P,
        KBP,
        [21],
        ["inspection"],
        "Inspection fee is ₦10,000. Call 0803 222 3344 to book a viewing of the 3 Bedroom Flat in Lekki Phase 1.",
    ),
    (
        P,
        KBP,
        [22],
        ["duplex size"],
        "4 Bedroom Terrace Duplex, Ikota: [[price:#22]]. 4 bedrooms, all en suite, 2 living rooms.",
    ),
    (
        P,
        KBP,
        [23],
        ["land size"],
        "500sqm Land in Ibeju-Lekki - [[price:#23]]. It's 10 minutes from the Lekki Free Trade Zone.",
    ),
    (
        P,
        KBP,
        [21, 24],
        ["options"],
        "We have the 3 Bedroom Flat in Lekki Phase 1 at [[price:#21]] and the 2 Bedroom Apartment, Yaba at [[price:#24]].",
    ),
    (
        P,
        KBP,
        [24],
        ["caution"],
        "The 2 Bedroom Apartment, Yaba is [[price:#24]]. Caution deposit is one year.",
    ),
    (
        P,
        KBP,
        [21],
        ["parking"],
        "Yes, the 3 Bedroom Flat in Lekki Phase 1 ([[price:#21]]) has parking for 2 cars and 24 hours power.",
    ),
    (
        E,
        KBE,
        [7],
        ["tv size"],
        "The Samsung 55 inch Smart TV is [[price:#7]]; it measures 123 cm wide.",
    ),
    (
        E,
        KBE,
        [5],
        ["ram"],
        "HP EliteBook 840 G5 ([[price:#5]]): Intel Core i5 8th Gen, 16GB RAM, 512GB SSD, 14 inch.",
    ),
]


def _blocked():
    bad = []
    for items, kb, matched, cust, reply in CASES:
        block = " ".join(i.description for i in items)  # descriptions are part of the real block
        g = GroundingResult(
            mode="detail", block=block, items=items, matched_seqs=list(matched),
            catalog_names=[i.name for i in items],
        )  # fmt: skip
        v = check_reply(reply, g, customer_texts=cust, kb_text=kb)
        if not v.ok:
            bad.append((reply, [x.reason for x in v.violations]))
    return bad


def test_the_set_is_forty_replies():
    assert len(CASES) == 40


def test_spec_heavy_replies_are_not_blocked_beyond_the_target():
    bad = _blocked()
    assert len(bad) / len(CASES) <= 0.03, bad
