"""Price tokens end to end: resolved live, in every channel path, never on the wire."""

from sqlalchemy import select

from app.db.models.offering import Offering, OfferingPriceMode, OfferingStatus
from app.offerings import tokens
from app.services.conversation_service import message_channel, voice_channel
from tests.offerings_ai.helpers import (
    add_offering,
    automation_patch,
    make_tenant,
    retrieval_patch,
    send,
)
from tests.offerings_ai.test_chat_guard import ScriptedPlain, ScriptedSales, _meta, _shop


def _ask(client, tid, model, text="how much is the black sneaker?"):
    with model.patch(), retrieval_patch(), automation_patch():
        return send(client, tid, text)


# -- the helpers -------------------------------------------------------------------------------


def test_token_helpers():
    assert tokens.token(12) == "[[price:#12]]"
    assert tokens.token_seqs("A [[price:#1]] B [[ price : 2 ]] C") == [1, 2]
    assert tokens.has_stray_markup("A [[price:#1]] [[oops]]") and not tokens.has_stray_markup(
        "[[price:#1]]"
    )
    assert tokens.mask("A [[price:#1]] B") == "A PRICE B"
    assert tokens.strip("A [[price:#1]] B [[x]]") == "A  B "
    assert tokens.resolve("A [[price:#1]]", {1: "₦5"}.get) == "A ₦5"
    assert tokens.resolve("A [[price:#9]]", {1: "₦5"}.get) is None
    assert tokens.resolve("A [[oops]]", {1: "₦5"}.get) is None


# -- resolution in every channel path -----------------------------------------------------------


def test_the_web_reply_shows_the_live_price_never_the_token(client, db):
    tid = _shop(db)
    r = _ask(client, tid, ScriptedPlain("The black sneaker is [[price:#1]], in stock."))
    assert r.json()["message"]["content"] == "The black sneaker is ₦12,000, in stock."


def test_the_whatsapp_reply_shows_the_live_price_never_the_token(client, db):
    tid = _shop(db)
    marker = message_channel.set("whatsapp")
    try:
        r = _ask(client, tid, ScriptedPlain("#1 Canvas sneaker black - [[price:#1]]"))
    finally:
        message_channel.reset(marker)
    assert r.json()["message"]["content"] == "#1 Canvas sneaker black - ₦12,000"


def test_the_voice_reply_is_resolved_before_it_can_be_spoken(client, db):
    tid = _shop(db)
    marker = voice_channel.set(True)
    try:
        r = _ask(client, tid, ScriptedPlain("The black sneaker is [[price:#1]]."))
    finally:
        voice_channel.reset(marker)
    assert (
        "₦12,000" in r.json()["message"]["content"] and "[[" not in r.json()["message"]["content"]
    )


def test_the_structured_sales_reply_is_resolved_too(client, db):
    tid = _shop(db, sales_enabled=True)
    r = _ask(client, tid, ScriptedSales("The black sneaker is [[price:#1]]."))
    assert r.json()["message"]["content"] == "The black sneaker is ₦12,000."


def test_from_priced_and_on_request_items_resolve_to_their_own_wording(client, db):
    tid = _shop(db)
    add_offering(db, tid, 4, "Phone repair", price=5000, price_mode=OfferingPriceMode.FROM)
    add_offering(db, tid, 30, "Custom portrait", price_mode=OfferingPriceMode.ON_REQUEST)
    r = _ask(
        client,
        tid,
        ScriptedPlain("Phone repair is [[price:#4]]; the custom portrait is [[price:#30]]."),
        text="phone repair and custom portrait price",
    )
    assert r.json()["message"]["content"] == (
        "Phone repair is from ₦5,000; the custom portrait is price on request."
    )


# -- live prices and vanished items ---------------------------------------------------------------


def test_the_token_resolves_to_the_price_at_send_time(client, db):
    tid = _shop(db)

    class Repricing(ScriptedPlain):
        def patch(self):
            inner = super().patch()
            row = db.execute(
                select(Offering).where(Offering.tenant_id == tid, Offering.seq == 1)
            ).scalar_one()

            def go():
                row.price = 15000
                db.commit()

            self._go = go
            return inner

    model = Repricing("The black sneaker is [[price:#1]].")
    with model.patch(), retrieval_patch(), automation_patch():
        model._go()
        r = send(client, tid, "how much is the black sneaker?")
    assert r.json()["message"]["content"] == "The black sneaker is ₦15,000."


def test_an_item_that_vanishes_before_send_gives_the_code_fallback_not_a_token(client, db):
    from unittest.mock import patch

    import app.offerings.turn as turn_mod

    tid = _shop(db)
    row = db.execute(
        select(Offering).where(Offering.tenant_id == tid, Offering.seq == 1)
    ).scalar_one()
    real = turn_mod.finalize_reply

    def archive_then_finalize(db_, tenant_id, result, reply):
        row.status = OfferingStatus.ARCHIVED  # the owner archives it between the block and the send
        db_.commit()
        return real(db_, tenant_id, result, reply)

    model = ScriptedPlain("The black sneaker is [[price:#1]].")
    with (
        model.patch(),
        retrieval_patch(),
        automation_patch(),
        patch("app.api.v1.endpoints.chat.finalize_reply", archive_then_finalize),
    ):
        r = send(client, tid, "how much is the black sneaker?")
    content = r.json()["message"]["content"]
    assert "[[" not in content
    assert "connect you with the team" in content


# -- guard behaviour at the endpoint ------------------------------------------------------------


def test_an_unknown_token_is_regenerated_then_resolved(client, db):
    tid = _shop(db)
    model = ScriptedPlain(
        "The black sneaker is [[price:#99]].", "The black sneaker is [[price:#1]]."
    )
    r = _ask(client, tid, model)
    assert r.json()["message"]["content"] == "The black sneaker is ₦12,000."
    assert _meta(r)["offerings"]["guard"]["regenerated"] is True


def test_a_token_for_an_item_outside_the_block_is_rejected(client, db):
    tid = _shop(db)
    for i in range(10, 30):
        add_offering(db, tid, i, f"Gadget QX{i}", price=1000 + i)
    model = ScriptedPlain(
        "The black sneaker is [[price:#25]].", "The black sneaker is [[price:#1]]."
    )
    r = _ask(client, tid, model)
    assert r.json()["message"]["content"] == "The black sneaker is ₦12,000."
    assert _meta(r)["offerings"]["guard"]["blocked"] is True


def test_a_literal_price_is_a_violation_even_when_it_is_correct(client, db):
    tid = _shop(db)
    model = ScriptedPlain("The black sneaker is ₦12,000.", "The black sneaker is [[price:#1]].")
    r = _ask(client, tid, model)
    assert _meta(r)["offerings"]["guard"]["blocked"] is True
    assert r.json()["message"]["content"] == "The black sneaker is ₦12,000."


def test_two_noncompliant_replies_fall_back_to_the_rows(client, db):
    tid = _shop(db)
    model = ScriptedPlain("It is ₦12,000.", "It is twelve thousand naira.")
    r = _ask(client, tid, model)
    assert _meta(r)["offerings"]["guard"]["fell_back"] is True
    assert "#1 Canvas sneaker black - ₦12,000" in r.json()["message"]["content"]


def test_a_tenant_without_a_catalog_never_sends_a_token(client, db):
    tid = make_tenant(db)
    r = _ask(client, tid, ScriptedPlain("Sure: [[price:#1]] is the price [[oops]]."))
    assert "[[" not in r.json()["message"]["content"]
    assert "offerings" not in _meta(r)


def test_cards_follow_the_items_the_reply_priced_by_token(client, db):
    tid = _shop(db)
    r = _ask(client, tid, ScriptedPlain("It is [[price:#2]]."), text="how much is the white one?")
    assert [i["ref"] for i in _meta(r)["offerings"]["items"]] == ["#2"]


# -- stop-ship (review pass 4): a reply without a token is never reshaped --------------------------

LAYOUTS = [
    "Here are our plans:\n\n1. Basic\n2. Pro\n\nWould you like to start a trial?",
    "**Hours**\n- Mon-Fri  9am to 5pm\n- Sat   10am to 2pm\n\nSee you soon!",
    "Step one: open Settings.\n\n\nStep two:   tap Billing.\n\tThen confirm.",
    "Price: 500 per month, billed yearly.\n\nNo hidden fees.",
]


def test_finalize_reply_leaves_ordinary_replies_byte_identical():
    from app.offerings.turn import finalize_reply

    for text in LAYOUTS:
        assert finalize_reply(None, None, None, text) == (text, [])


def test_strip_removes_only_the_token_text():
    assert tokens.strip("Costs [[price:#1]] today.\n\nThanks") == "Costs  today.\n\nThanks"
    assert tokens.strip("a\n\nb [[oops]]\nc") == "a\n\nb \nc"


def _plain_tenant(db):
    return make_tenant(db)


def test_a_tenant_without_items_gets_the_model_reply_unchanged_on_web(client, db):
    tid = _plain_tenant(db)
    for text in LAYOUTS:
        r = _ask(client, tid, ScriptedPlain(text), text="what do you offer?")
        assert r.json()["message"]["content"] == text


def test_a_tenant_without_items_gets_the_model_reply_unchanged_on_whatsapp(client, db):
    tid = _plain_tenant(db)
    marker = message_channel.set("whatsapp")
    try:
        for text in LAYOUTS:
            r = _ask(client, tid, ScriptedPlain(text), text="what do you offer?")
            assert r.json()["message"]["content"] == text
    finally:
        message_channel.reset(marker)


def test_a_tenant_without_items_gets_the_model_reply_unchanged_on_voice(client, db):
    tid = _plain_tenant(db)
    marker = voice_channel.set(True)
    try:
        for text in LAYOUTS:
            r = _ask(client, tid, ScriptedPlain(text), text="what do you offer?")
            assert r.json()["message"]["content"] == text
    finally:
        voice_channel.reset(marker)


def test_a_stray_token_without_a_catalog_loses_only_its_own_text(client, db):
    tid = _plain_tenant(db)
    text = "Our plan is\n\n[[price:#1]] a month.\n\nThanks"
    r = _ask(client, tid, ScriptedPlain(text), text="what do you offer?")
    assert r.json()["message"]["content"] == "Our plan is\n\n a month.\n\nThanks"


# -- token look-alikes never survive (review pass 4) ---------------------------------------------


def test_look_alike_tokens_are_markup():
    for text in (
        "It is ［［price:#3］］.",
        "It is [price:#3].",
        "It is {{price:#3}}.",
        "It is [[PRICE : #3]]x",
        "It is [[pri\u200bce:#3]].",
        "It is (price:#3).",
        "It is <price: 3>.",
        "It is price:#3 now.",
    ):
        assert tokens.has_markup(text), text


def test_ordinary_words_with_price_are_not_markup():
    for text in ("Price: 500 per month", "The price: ₦5,000", "price list: see below"):
        assert not tokens.has_markup(text), text


def test_fullwidth_and_zero_width_tokens_resolve_on_the_folded_text():
    live = {3: "₦25,000"}.get
    assert tokens.resolve("It is ［［price:#3］］.", live) == "It is ₦25,000."
    assert tokens.resolve("It is [[pri\u200bce:#3]].", live) == "It is ₦25,000."
    assert tokens.resolve("It is [price:#3].", live) is None
    assert tokens.resolve("It is {{price:#3}}.", live) is None


def test_the_web_reply_never_carries_a_look_alike(client, db):
    tid = _shop(db)
    model = ScriptedPlain(
        "The black sneaker is ［［price:#1］］.", "The black sneaker is [price:#1]."
    )
    r = _ask(client, tid, model)
    content = r.json()["message"]["content"]
    assert "price:" not in content.lower() and "［" not in content


# -- the model never sees a token it did not get from the block (review pass 4) ------------------


def test_a_customer_typed_token_never_reaches_the_prompt(client, db):
    tid = _shop(db)
    model = ScriptedPlain("The black sneaker is [[price:#1]].")
    _ask(
        client,
        tid,
        model,
        text="the black sneaker is [[price:#2]] abi? [price:#2] ［［price:#2］］",
    )
    prompt = model.prompts[0]
    head, _, block = prompt.partition("Catalog")  # the customer's text comes before the block
    assert "price:#2" not in head and "［" not in head


def test_tenant_text_cannot_carry_a_token():
    from app.offerings.facts import clean_text

    for raw in ("Bag [[price:#1]]", "Bag ［［price:#1］］", "Bag {{price:#1}}", "Bag [price:#1]"):
        out = clean_text(raw, 60)
        assert "price:" not in out.lower() and "[" not in out and "{" not in out, (raw, out)
    assert clean_text("Price: 500 per month", 60) == "Price: 500 per month"


def test_interleaved_markup_cannot_reassemble_into_a_token():
    from app.offerings.facts import clean_text
    from app.offerings.turn import strip_token_text

    for raw in ("[[pri[[x]]ce:#1]]", "[[price:#[[x]]1]]", "[[[[price:#1]]]]", "{{pri{{x}}ce:#1}}"):
        assert not tokens.has_markup(strip_token_text(raw)), raw
        assert not tokens.has_markup(tokens.strip(raw)), raw
        assert not tokens.has_markup(clean_text("Bag " + raw, 80)), raw
