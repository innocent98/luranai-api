"""T5: which items a reply is about, `meta.offerings`, session memory and the miss log."""

import json
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app.db.models.conversation import ConversationSession
from app.db.models.offering import Offering, OfferingImage, OfferingMiss
from app.offerings.grounding import GroundingResult
from app.offerings.writer import shown_items
from tests.offerings_ai.helpers import (
    add_offering,
    automation_patch,
    make_tenant,
    retrieval_patch,
    send,
)
from tests.offerings_ai.test_chat_guard import ScriptedPlain, ScriptedSales, _shop
from tests.offerings_ai.test_guard import ITEMS


def _g(items=ITEMS, mode="detail", matched=()):
    return GroundingResult(mode=mode, block="", items=list(items), matched_seqs=list(matched))


# -- selection -------------------------------------------------------------------------------


def test_sales_mode_uses_the_structured_refs_filtered_to_injected_items():
    sig = {"catalog_refs": ["#2", "#99", "#1"]}
    assert [i.seq for i in shown_items(_g(), "ok", sales_signals=sig)] == [2, 1]


def test_sales_mode_caps_at_five_items():
    sig = {"catalog_refs": [f"#{n}" for n in (1, 2, 3, 4, 5, 6)]}
    assert len(shown_items(_g(), "x", sales_signals=sig)) == 5


def test_plain_mode_matches_ref_or_full_name_in_the_reply():
    out = shown_items(_g(), "The Canvas sneaker black is great, also #3.", sales_signals=None)
    assert sorted(i.seq for i in out) == [1, 3]


def test_plain_mode_does_not_match_a_partial_name():
    assert shown_items(_g(), "We have sneakers and bags.", sales_signals=None) == []


def test_sales_mode_with_empty_refs_falls_back_to_the_reply_text():
    out = shown_items(_g(), "The Leather bag is ₦45,000.", sales_signals={"catalog_refs": []})
    assert [i.seq for i in out] == [3]


def test_fallback_reply_shows_the_matched_items():
    g = _g(matched=[1, 2])
    out = shown_items(g, "Here is what we have listed:\n#1 ...", sales_signals=None, fell_back=True)
    assert [i.seq for i in out] == [1, 2]


def test_no_injected_items_means_no_cards():
    assert (
        shown_items(_g(items=[]), "The black sneaker", sales_signals={"catalog_refs": ["#1"]}) == []
    )


# -- through the endpoint -----------------------------------------------------------------


def _cover(db, tid, seq):
    row = db.execute(
        select(Offering).where(Offering.tenant_id == tid, Offering.seq == seq)
    ).scalar_one()
    img = OfferingImage(
        id=uuid4(),
        offering_id=row.id,
        tenant_id=tid,
        url="https://res.cloudinary.com/demo/image/upload/v1/offerings/x.jpg",
        cloudinary_public_id="offerings/x",
        position=0,
    )
    db.add(img)
    db.add(
        OfferingImage(
            id=uuid4(),
            offering_id=row.id,
            tenant_id=tid,
            url="https://res.cloudinary.com/demo/b.jpg",
            cloudinary_public_id="offerings/y",
            position=1,
        )
    )
    ids = (row.id, img.id, img.url)
    db.commit()
    return ids


def _offerings_meta(r):
    return r.json()["message"]["meta"]["offerings"]


def test_meta_items_follow_the_frozen_contract(client, db):
    tid = _shop(db, sales_enabled=True)
    row_id, img_id, img_url = _cover(db, tid, 1)
    sales = ScriptedSales("The black sneaker is ₦12,000.")
    sales.signals = {"catalog_refs": ["#1"]}
    with _with_refs(sales, ["#1"]), retrieval_patch(), automation_patch():
        r = send(client, tid, "how much is the black sneaker?")
    meta = _offerings_meta(r)
    assert meta["mode"] == "detail"
    assert meta["items"] == [
        {
            "ref": "#1",
            "offering_id": str(row_id),
            "name": "Canvas sneaker black",
            "price_text": "₦12,000",
            "availability": "in_stock",
            "cover_image_id": str(img_id),
            "cover_url": img_url,
            "photo_count": 2,
        }
    ]
    assert meta["shown_at"] and set(meta["guard"]) == {
        "checked",
        "blocked",
        "regenerated",
        "fell_back",
    }


def _with_refs(sales: ScriptedSales, refs):
    async def fake(_self, prompt, config, **kwargs):
        sales.calls.append({"prompt": prompt.full_prompt, **kwargs})
        signals = {
            "asked_features": False,
            "requested_demo": False,
            "mentioned_competitor": False,
            "ready_to_buy": False,
            "offer_meeting": False,
            "offer_quote": False,
            "catalog_refs": refs,
            "cart": [],
            "offered_price": None,
        }
        return (
            sales.replies.pop(0),
            {"intent": "PRICING_INQUIRY", "captured": {}, "signals": signals},
            100,
            False,
        )

    return patch("app.services.rag_service.RAGService.generate_sales_response", new=fake)


def test_plain_mode_cards_come_from_names_in_the_reply(client, db):
    tid = _shop(db)
    model = ScriptedPlain("We have the Canvas sneaker white at ₦13,500.")
    with model.patch(), retrieval_patch(), automation_patch():
        r = send(client, tid, "show me the white sneaker")
    assert [i["ref"] for i in _offerings_meta(r)["items"]] == ["#2"]
    assert _offerings_meta(r)["items"][0]["cover_url"] is None
    assert _offerings_meta(r)["items"][0]["photo_count"] == 0


def test_tenant_without_items_still_has_no_offerings_meta(client, db):
    tid = make_tenant(db)
    model = ScriptedPlain("Hello there, how can I help you today friend?")
    with model.patch(), retrieval_patch(), automation_patch():
        r = send(client, tid, "hello")
    assert "offerings" not in r.json()["message"]["meta"]


def test_session_remembers_what_was_shown_and_the_next_turn_uses_it(client, db):
    tid = _shop(db)
    for i in range(3, 20):
        add_offering(db, tid, i, f"Filler thing {i}", price=900 + i)
    model = ScriptedPlain("The Canvas sneaker white is ₦13,500.", "It is ₦13,500.")
    with model.patch(), retrieval_patch(), automation_patch():
        r1 = send(client, tid, "show me the white sneaker")
        sid = r1.json()["session_id"]
        session = db.execute(
            select(ConversationSession).where(ConversationSession.id == UUID(sid))
        ).scalar_one()
        assert session.meta["offerings_shown"] == [2]
        send(client, tid, "how much is it?", session_id=sid)
    assert '<offering ref="#2">' in model.prompts[1]


def test_shown_memory_keeps_the_last_five_and_survives_an_escalation_offer(client, db):
    tid = _shop(db)
    model = ScriptedPlain(
        "It is ₦11,000.", "I can do ₦10,000."
    )  # guard falls back -> offer escalation
    with model.patch(), retrieval_patch(), automation_patch():
        r = send(client, tid, "how much is the black sneaker?")
    sid = r.json()["session_id"]
    session = db.execute(
        select(ConversationSession).where(ConversationSession.id == UUID(sid))
    ).scalar_one()
    db.refresh(session)
    assert session.meta.get("pending_escalation") is True
    assert session.meta["offerings_shown"] == [1, 2]  # both sneakers match, both were listed


# -- misses --------------------------------------------------------------------------------


def _misses(db, tid):
    return {
        m.term: m.count
        for m in db.execute(select(OfferingMiss).where(OfferingMiss.tenant_id == tid)).scalars()
    }


def _ask(client, db, tid, text, user="v1", session_id=None):
    model = ScriptedPlain(
        "I don't have that listed. Would you like me to connect you with the team?"
    )
    with model.patch(), retrieval_patch(), automation_patch():
        return send(client, tid, text, user=user, session_id=session_id)


def test_not_listed_item_is_counted_as_a_normalised_term(client, db):
    tid = _shop(db)
    for i in range(3, 20):
        add_offering(db, tid, i, f"Filler thing {i}", price=900 + i)
    _ask(client, db, tid, "Do you sell HELICOPTERS?? my number is 08012345678", user="v1")
    assert _misses(db, tid) == {"helicopter": 1}
    _ask(client, db, tid, "do you have a helicopter", user="v2")
    assert _misses(db, tid) == {"helicopter": 2}
    row = db.execute(select(OfferingMiss)).scalars().first()
    assert row.first_seen_at and row.last_seen_at
    assert "08012345678" not in json.dumps(
        [m.term for m in db.execute(select(OfferingMiss)).scalars()]
    )


@pytest.mark.parametrize(
    "text", ["hello", "thanks!", "what do you have?", "how much is the black sneaker"]
)
def test_chatter_browse_and_hits_are_not_misses(client, db, text):
    tid = _shop(db)
    for i in range(3, 20):
        add_offering(db, tid, i, f"Filler thing {i}", price=900 + i)
    _ask(client, db, tid, text)
    assert _misses(db, tid) == {}


def test_misses_are_per_tenant(client, db):
    a, b = _shop(db), _shop(db)
    for t in (a, b):
        for i in range(3, 20):
            add_offering(db, t, i, f"Filler thing {i}", price=900 + i)
    _ask(client, db, a, "do you sell helicopters")
    assert _misses(db, a) == {"helicopter": 1} and _misses(db, b) == {}


def test_miss_write_failure_never_breaks_the_reply(client, db):
    tid = _shop(db)
    for i in range(3, 20):
        add_offering(db, tid, i, f"Filler thing {i}", price=900 + i)
    with patch("app.offerings.misses._upsert", side_effect=RuntimeError("db")):
        r = _ask(client, db, tid, "do you sell helicopters")
    assert r.status_code == 200
