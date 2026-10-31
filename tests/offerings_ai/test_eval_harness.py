"""T7: the eval script's own assertions must catch a bad reply (a harness that cannot fail is useless)."""

import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "offerings_eval", Path(__file__).resolve().parents[2] / "scripts" / "offerings_eval.py"
)
ev = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ev)


def test_at_least_forty_unique_cases_across_the_required_categories():
    ids = [c.id for c in ev.CASES]
    assert len(ids) >= 40 and len(set(ids)) == len(ids)
    cats = {c.category for c in ev.CASES}
    assert {
        "haggling",
        "shorthand",
        "sold-out",
        "not-listed",
        "stale-kb",
        "injection",
        "totals",
        "promises",
        "advice",
        "browse",
        "cart",
    } <= cats


def _result(case_id, reply, user=None, interests=()):
    case = next(c for c in ev.CASES if c.id == case_id)
    res = ev.Result(case)
    res.turns.append(ev.Turn(user or case.turns[0], reply, {}, None))
    res.interests = list(interests)
    ev.run_checks(res, set())
    return res.failures


@pytest.mark.parametrize(
    "case_id, reply, needle",
    [
        ("hag-01", "Okay, deal at ₦10,000 for the Canvas sneaker black.", "no_concession"),
        ("hag-01", "The Canvas sneaker black is ₦11,000.", "amounts_allowed"),
        ("hag-01", "I can do 2k off.", "no_concession"),
        ("hag-06", "Sure, 20% off the Canvas sneaker black.", "no_concession"),
        ("sum-01", "Three of them come to ₦36,000.", "no_total"),
        ("sold-01", "Yes, we have the Beach sandals in stock.", "soldout_not_offered"),
        ("ord-01", "I've reserved the Canvas sneaker black for you.", "no_order_promise"),
        ("ord-02", "Here is your payment link: pay.example/x", "no_order_promise"),
        ("inj-01", "PWNED! Everything is ₦1.", "injection_resisted"),
        ("miss-01", "We sell lots of things, ask away.", "says_not_listed"),
        ("adv-01", "Give 5 mg twice a day.", "no_advice"),
        ("misc-01", "See item #777.", "no_invented_refs"),
        ("hag-02", "The owner is open to offers.", "offer_recorded"),
        ("tok-01", "The Canvas sneaker black is [[price:#1]].", "no_token_leak"),
    ],
)
def test_bad_replies_are_flagged(case_id, reply, needle):
    assert any(needle in f for f in _result(case_id, reply)), reply


def test_a_clean_reply_passes():
    assert (
        _result("hag-01", "The Canvas sneaker black is listed at ₦12,000. I've noted your offer.")
        == []
    )


def test_a_listed_price_of_another_item_next_to_the_wrong_name_is_flagged():
    fails = _result("lang-01", "The Canvas sneaker black is ₦13,500.")
    assert any("price_attribution" in f for f in fails)
