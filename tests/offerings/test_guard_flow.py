"""T4: regenerate once, then fall back to a code-built reply; every outcome is recorded."""

import pytest

from app.db.models.offering import OfferingAvailability, OfferingPriceMode
from app.offerings.fallback import fallback_reply
from app.offerings.grounding import GroundingResult
from app.offerings.guard_flow import CORRECTIVE_NOTE, guard_reply
from tests.offerings.test_guard import ITEMS, _grounding, _item

GOOD = "The black sneaker is ₦12,000."
BAD = "The black sneaker is ₦11,000."


def _regen(*replies, calls=None):
    seq = list(replies)

    async def regenerate(note):
        if calls is not None:
            calls.append(note)
        text, meta, tokens, err = seq.pop(0)
        return text, meta, tokens, err

    return regenerate


async def _run(reply, regenerate, grounding=None, **kw):
    return await guard_reply(
        reply, grounding or _grounding(), regenerate=regenerate, tokens=100, **kw
    )


@pytest.mark.asyncio
async def test_clean_reply_is_untouched_and_recorded():
    calls = []
    out = await _run(GOOD, _regen(calls=calls))
    assert out.reply == GOOD and out.tokens == 100 and calls == []
    assert out.record == {
        "checked": True,
        "blocked": False,
        "regenerated": False,
        "fell_back": False,
    }
    assert out.sales_meta is None and out.offer_escalation is False


@pytest.mark.asyncio
async def test_violation_regenerates_once_with_the_corrective_line():
    calls = []
    out = await _run(BAD, _regen((GOOD, {"intent": "X"}, 80, False), calls=calls))
    assert out.reply == GOOD
    assert out.tokens == 180  # both calls count toward the turn
    assert out.sales_meta == {"intent": "X"}
    assert out.record == {"checked": True, "blocked": True, "regenerated": True, "fell_back": False}
    assert calls == [CORRECTIVE_NOTE]
    assert "only the prices" in CORRECTIVE_NOTE.lower()


@pytest.mark.asyncio
async def test_second_violation_falls_back_to_code_reply():
    out = await _run(BAD, _regen(("Now it is ₦10,500.", {"intent": "Y"}, 60, False)))
    assert out.record == {"checked": True, "blocked": True, "regenerated": True, "fell_back": True}
    assert out.offer_escalation is True
    assert out.tokens == 160
    assert "₦12,000" in out.reply and "₦11,000" not in out.reply and "10,500" not in out.reply


@pytest.mark.asyncio
async def test_regeneration_api_error_falls_back():
    out = await _run(BAD, _regen(("I'm having trouble", None, None, True)))
    assert out.record["fell_back"] is True and out.record["regenerated"] is True
    assert out.tokens == 100


@pytest.mark.asyncio
async def test_regeneration_exception_falls_back():
    async def boom(note):
        raise RuntimeError("openai down")

    out = await _run(BAD, boom)
    assert out.record["fell_back"] is True and "₦12,000" in out.reply


@pytest.mark.asyncio
async def test_strict_mode_uses_generic_code_reply_without_prices():
    g = _grounding(strict=True)
    out = await _run("The black sneaker is ₦12,000.", _regen(("It is ₦12,000", None, 5, False)), g)
    assert out.record["fell_back"] is True
    assert "₦" not in out.reply and "team" in out.reply.lower()


@pytest.mark.asyncio
async def test_sales_meta_of_the_blocked_reply_is_kept_when_falling_back():
    out = await _run(BAD, _regen((BAD, {"intent": "Z"}, 10, False)), sales_meta={"intent": "FIRST"})
    assert out.record["fell_back"] is True
    assert out.sales_meta == {
        "intent": "Z"
    }  # the latest structured output still carries `captured`


@pytest.mark.asyncio
async def test_customer_amounts_and_kb_are_passed_to_the_guard():
    out = await _run(
        "You said ₦20,000. Delivery in Lagos is ₦2,000.",
        _regen(),
        customer_texts=("my budget is ₦20,000",),
        kb_text="Delivery in Lagos costs ₦2,000.",
    )
    assert out.record["blocked"] is False


# -- the code fallback reply -------------------------------------------------------------


def test_fallback_for_detail_lists_matched_items_from_rows():
    g = GroundingResult(
        mode="detail", block="", items=ITEMS[:3], matched_seqs=[1, 2], currency="NGN"
    )
    text = fallback_reply(g)
    assert "#1 Canvas sneaker black" in text and "₦12,000" in text and "₦13,500" in text
    assert "Leather bag" not in text  # only the items the customer asked about
    assert text.rstrip().endswith("?") and "team" in text.lower()


def test_fallback_for_browse_lists_the_menu_and_marks_sold_out():
    g = GroundingResult(mode="browse", block="", items=ITEMS, currency="NGN", total_active=40)
    text = fallback_reply(g)
    assert "#5 Sold out sandals" in text and "sold out" in text.lower()
    assert "from ₦5,000 per phone" in text and "price on request" in text
    assert "more items" in text  # 6 shown of 40


def test_fallback_for_no_match_says_not_listed():
    g = GroundingResult(mode="none", block="", items=[], currency="NGN")
    assert "don't have that listed" in fallback_reply(g)


def test_fallback_has_no_amount_outside_the_rows():
    from app.offerings.guard import check_reply

    for mode, matched in (("detail", [1, 3]), ("browse", [])):
        g = GroundingResult(mode=mode, block="", items=ITEMS, matched_seqs=matched, currency="NGN")
        assert check_reply(fallback_reply(g), g).ok


def test_fallback_whatsapp_has_no_markdown():
    g = GroundingResult(mode="browse", block="", items=ITEMS, currency="NGN")
    assert "*" not in fallback_reply(g) and "#" in fallback_reply(g)


def test_fallback_marks_unavailable_service_as_fully_booked():
    from app.db.models.offering import OfferingKind

    booked = _item(
        7,
        "Haircut",
        3000,
        kind=OfferingKind.SERVICE,
        availability=OfferingAvailability.OUT_OF_STOCK,
    )
    g = GroundingResult(mode="detail", block="", items=[booked], matched_seqs=[7], currency="NGN")
    assert "fully booked" in fallback_reply(g).lower()
    assert OfferingPriceMode.FIXED  # imported for parity with the other tests
