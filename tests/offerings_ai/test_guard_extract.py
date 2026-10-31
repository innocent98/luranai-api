"""T4: amount extraction (Nigerian forms) for the price guard. Table-driven."""

from decimal import Decimal as D

import pytest

from app.offerings.guard import extract_amounts


def _vals(text):
    return [(a.value, a.kind) for a in extract_amounts(text)]


M = "money"
P = "percent"

CASES = [
    # symbols and codes
    ("It costs ₦25,000.", [(D(25000), M)]),
    ("It costs ₦ 25,000 today", [(D(25000), M)]),
    ("N25,000 only", [(D(25000), M)]),
    ("N25000", [(D(25000), M)]),
    ("n25000 abeg", [(D(25000), M)]),
    ("NGN 25,000", [(D(25000), M)]),
    ("NGN25k", [(D(25000), M)]),
    ("₦12000", [(D(12000), M)]),
    ("₦2,500.50", [(D("2500.50"), M)]),
    ("N2,500.50", [(D("2500.50"), M)]),
    ("₦12,000.00", [(D(12000), M)]),
    ("$25", [(D(25), M)]),
    ("USD 25", [(D(25), M)]),
    ("£1,200", [(D(1200), M)]),
    ("€9.99", [(D("9.99"), M)]),
    # suffix currency words
    ("25,000 naira", [(D(25000), M)]),
    ("25000 naira", [(D(25000), M)]),
    ("500 naira", [(D(500), M)]),
    ("25,000 NGN", [(D(25000), M)]),
    ("20 dollars", [(D(20), M)]),
    ("2,500 kobo", [(D(25), M)]),
    # shorthand
    ("25k", [(D(25000), M)]),
    ("25K please", [(D(25000), M)]),
    ("2k", [(D(2000), M)]),
    ("1.5k", [(D(1500), M)]),
    ("2.5m", []),
    ("2.5m naira", [(D(2500000), M)]),
    ("N2.5m", [(D(2500000), M)]),
    ("2.5 million", [(D(2500000), M)]),
    ("25 thousand", [(D(25000), M)]),
    ("3 mil", [(D(3000000), M)]),
    ("₦1.2b", [(D(1200000000), M)]),
    # ranges and lists
    ("₦10,000 - ₦12,000", [(D(10000), M), (D(12000), M)]),
    ("₦10,000-12,000", [(D(10000), M), (D(12000), M)]),
    ("10k-12k", [(D(10000), M), (D(12000), M)]),
    ("10-12k", [(D(10000), M), (D(12000), M)]),
    ("between 10k and 12k", [(D(10000), M), (D(12000), M)]),
    ("₦5,000, ₦6,000 and ₦7,000", [(D(5000), M), (D(6000), M), (D(7000), M)]),
    # grouped or large bare numbers are money
    ("The price is 12,000", [(D(12000), M)]),
    ("price: 12000", [(D(12000), M)]),
    ("it is 1,200,000 in total", [(D(1200000), M)]),
    ("₦12 000", [(D(12000), M)]),
    ("12 000 naira", [(D(12000), M)]),
    # bare small numbers after a price verb
    ("it costs 500", [(D(500), M)]),
    ("the price is 750 only", [(D(750), M)]),
    ("I can do 900 for it", [(D(900), M)]),
    # percentages
    ("10% off", [(D(10), P)]),
    ("a 5 percent discount", [(D(5), P)]),
    ("7.5% VAT", [(D("7.5"), P)]),
    # number words
    ("twelve thousand naira", [(D(12000), M)]),
    ("fifteen thousand", [(D(15000), M)]),
    ("twenty-five thousand naira", [(D(25000), M)]),
    ("two million naira", [(D(2000000), M)]),
    ("one thousand five hundred naira", [(D(1500), M)]),
    ("five hundred naira", [(D(500), M)]),
    ("two hundred and fifty naira", [(D(250), M)]),
    # compat forms, spacing and year-vs-price
    ("12 k", [(D(12000), M)]),
    ("₦12,\u200b000", [(D(12000), M)]),
    ("₦１２,０００", [(D(12000), M)]),
    ("starts from 2000", [(D(2000), M)]),
    ("price of 2000", [(D(2000), M)]),
    ("₦12.000", [(D(12), M)]),
    # second Opus pass
    ("The price is #9,000", [(D(9000), M)]),
    ("#9000", [(D(9000), M)]),
    ("#9k", [(D(9000), M)]),
    ("between 9 and 12 thousand", [(D(9000), M), (D(12000), M)]),
    ("9 or 10k", [(D(9000), M), (D(10000), M)]),
    ("9 thou", [(D(9000), M)]),
    ("nine-k", [(D(9000), M)]),
    ("two thousand five hundred", [(D(2500), M)]),
    # units and bare numbers are not money
    ("water resistant to 50m", []),
    ("Plot 1234, Admiralty Way", []),
    ("We have served over 2000 customers", []),
    ("100% cotton", [(D(100), P)]),
    ("Model 5100 is out", []),
    ("Red Bag: 25000", [(D(25000), M)]),
    ("Red Bag - 25,000", [(D(25000), M)]),
    ("The Red Bag is 25,000", [(D(25000), M)]),
    ("25,000 only", [(D(25000), M)]),
    # NOT money
    ("We have 2 pairs left", []),
    ("Size 42 is available", []),
    ("In 2026 we opened", []),
    ("Call 08012345678", []),
    ("Call +2348012345678", []),
    ("Open 9:00 to 17:30", []),
    ("Item #12 and #3", []),
    ("Item #3 costs", []),
    ("It is 5m wide", []),
    ("A 25 kg bag", []),
    ("Order 3 items for 2 people", []),
    ("About 40 photos", []),
    ("We are open 24/7", []),
    ("Take 2 of them", []),
    ("That is 1 of 12", []),
    ("Version 2.5 is out", []),
    ("I have been here for 10 years", []),
    ("in 20 minutes", []),
    ("Account 0123456789", []),
]


@pytest.mark.parametrize("text, expected", CASES, ids=[c[0] for c in CASES])
def test_extract_amounts(text, expected):
    assert _vals(text) == expected


def test_currency_marker_is_recorded():
    assert extract_amounts("₦100")[0].currency == "NGN"
    assert extract_amounts("100 naira")[0].currency == "NGN"
    assert extract_amounts("$100")[0].currency == "USD"
    assert extract_amounts("100k")[0].currency is None


def test_at_least_sixty_extraction_cases():
    assert len(CASES) >= 60
