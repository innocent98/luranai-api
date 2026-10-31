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


def _browse_grounding():
    g = _grounding()
    g.items = [i for i in g.items if i.price != 18000]  # the messages offer 18k: not a listed price
    g.matched_seqs = []
    g.mode = "browse"
    return g


def test_realistic_haggling_is_caught_with_the_item_matched(grounding):
    from tests.offerings_ai.bargain_sets import HAGGLING_REAL

    missed = [m for m in HAGGLING_REAL if not is_offer_turn(m, grounding, False)]
    assert 1 - len(missed) / len(HAGGLING_REAL) >= 0.90, missed


def test_realistic_ordinary_messages_are_not_offers_with_the_item_matched(grounding):
    from tests.offerings_ai.bargain_sets import ORDINARY_REAL

    wrong = [m for m in ORDINARY_REAL if is_offer_turn(m, grounding, False)]
    assert len(wrong) / len(ORDINARY_REAL) <= 0.03, wrong


def test_figure_only_offers_are_caught_when_nothing_was_matched():
    from tests.offerings_ai.bargain_sets import HAGGLING_REAL

    g = _browse_grounding()
    missed = [m for m in HAGGLING_REAL if not is_offer_turn(m, g, False)]
    assert 1 - len(missed) / len(HAGGLING_REAL) >= 0.90, missed


def test_ordinary_messages_stay_ordinary_when_nothing_was_matched():
    from tests.offerings_ai.bargain_sets import ORDINARY_REAL

    g = _browse_grounding()
    wrong = [m for m in ORDINARY_REAL if is_offer_turn(m, g, False)]
    assert len(wrong) / len(ORDINARY_REAL) <= 0.03, wrong


def test_context_prices_give_a_figure_something_to_compare_with():
    g = _browse_grounding()
    assert is_offer_turn("abeg 9000", g, False, context_prices=lambda: [25000])
    assert not is_offer_turn("abeg 30000", g, False, context_prices=lambda: [25000])


def test_a_payment_plan_is_not_an_offer(grounding):
    assert not is_offer_turn("Can I pay 12,500 now and the balance on delivery?", grounding, False)
    assert not is_offer_turn("I'll pay 10k deposit and the rest later", grounding, False)


def test_a_time_of_day_is_not_a_figure_but_an_amount_after_at_is(grounding):
    assert is_offer_turn("Can you sell it at 20,000?", grounding, False)
    assert is_offer_turn("I can pay around 20,000", grounding, False)
    assert not is_offer_turn("Can you deliver at 5pm?", grounding, False)


def test_adversarial_numbers_are_bounded_and_never_raise(grounding):
    import time

    start = time.perf_counter()
    for text in ("1 " * 5000, "#" + "1" * 4400, "1," * 5000, "1k " * 3000, "₦1 " * 3000):
        is_offer_turn(text, grounding, False)
    assert time.perf_counter() - start < 1.0


def test_another_items_price_is_still_an_offer_when_nothing_was_matched():
    g = _browse_grounding()
    assert is_offer_turn(
        "I'll pay 12,000", g, False
    )  # 12,000 is the sneaker's price, not this one's
    assert not is_offer_turn("is it ₦25,000 for each?", g, False)
    assert not is_offer_turn("does that cost 25k?", g, False)


def test_html_entity_tokens_are_caught_and_removed():
    from app.offerings import markup

    text = "The Black Sneaker is &#91;&#91;price:#1&#93;&#93;."
    assert markup.has_price_markup(text)
    assert not markup.has_price_markup(markup.strip_price_markup(text))
