"""Offer-turn detection both ways: bargaining is caught, ordinary questions are left to the model."""

import pytest

from app.offerings.offer_turn import is_offer_turn
from tests.offerings_ai.bargain_sets import HAGGLING, ORDINARY
from tests.offerings_ai.test_guard import _grounding


@pytest.fixture
def grounding():
    return _grounding(matched=(11,))  # the red bag, listed at 25,000


def test_haggling_messages_are_caught(grounding):
    missed = [m for m in HAGGLING if not is_offer_turn(m, grounding, False)]
    rate = 1 - len(missed) / len(HAGGLING)
    assert rate >= 0.95, missed


def test_ordinary_messages_are_not_offers(grounding):
    wrong = [m for m in ORDINARY if is_offer_turn(m, grounding, False)]
    assert len(wrong) / len(ORDINARY) <= 0.03, wrong


@pytest.mark.parametrize(
    "message",
    [
        "can you do delivery to Lekki?",
        "how about the red one?",
        "can you do size 44?",
        "is it 100% cotton?",
    ],
)
def test_bargaining_looking_verbs_alone_do_not_trigger_code_only_replies(grounding, message):
    assert not is_offer_turn(message, grounding, False)


def test_a_bare_figure_below_the_price_is_an_offer_but_one_above_is_not(grounding):
    assert is_offer_turn("abeg 9000", grounding, False)
    assert not is_offer_turn("abeg 30000", grounding, False)


def test_the_model_signal_alone_makes_it_an_offer_turn(grounding):
    assert is_offer_turn("ok", grounding, True)


def test_figures_next_to_units_are_never_offers(grounding):
    for m in ("size 9000", "order number is 10021", "I need 12 pieces", "for 150 guests", "x 3"):
        assert not is_offer_turn(m, grounding, False), m


def test_the_guard_reads_figures_next_to_units_as_money_so_nothing_hides_an_offer():
    from app.offerings.amounts import customer_amounts

    assert customer_amounts("size 9000") == []
    assert [a.value for a in customer_amounts("size 9000", strict=True)] == [9000]
