"""T4 through the real endpoint: the guard sits between the model and the stored reply."""

import json
from unittest.mock import patch

from sqlalchemy import select

from app.db.models.conversation import ConversationSession
from tests.offerings_ai.helpers import (
    add_offering,
    automation_patch,
    make_tenant,
    retrieval_patch,
    send,
)

_SIGNALS = {
    "asked_features": False,
    "requested_demo": False,
    "mentioned_competitor": False,
    "ready_to_buy": False,
    "offer_meeting": False,
    "offer_quote": False,
    "catalog_refs": [],
    "cart": [],
    "offered_price": None,
}


def _shop(db, **kw):
    tid = make_tenant(db, **kw)
    add_offering(db, tid, 1, "Canvas sneaker black", price=12000, category="Sneakers")
    add_offering(db, tid, 2, "Canvas sneaker white", price=13500, category="Sneakers")
    return tid


class ScriptedPlain:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.prompts: list[str] = []

    def patch(self):
        async def fake(
            _self, prompt, max_tokens=800, model_name=None, system_message=None, image_urls=None
        ):
            self.prompts.append(prompt.full_prompt)
            return self.replies.pop(0), 100, False

        return patch("app.services.rag_service.RAGService.generate_ai_response", new=fake)


class ScriptedSales:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls: list[dict] = []

    def patch(self):
        async def fake(_self, prompt, config, **kwargs):
            self.calls.append({"prompt": prompt.full_prompt, **kwargs})
            return (
                self.replies.pop(0),
                {"intent": "PRICING_INQUIRY", "captured": {}, "signals": dict(_SIGNALS)},
                100,
                False,
            )

        return patch("app.services.rag_service.RAGService.generate_sales_response", new=fake)


def _meta(r):
    return r.json()["message"]["meta"]


def test_clean_reply_is_sent_unchanged_and_recorded(client, db):
    tid = _shop(db)
    model = ScriptedPlain("The black sneaker is ₦12,000.")
    with model.patch(), retrieval_patch(), automation_patch():
        r = send(client, tid, "how much is the black sneaker?")
    assert r.json()["message"]["content"] == "The black sneaker is ₦12,000."
    assert _meta(r)["offerings"]["guard"] == {
        "checked": True,
        "blocked": False,
        "regenerated": False,
        "fell_back": False,
    }
    assert len(model.prompts) == 1


def test_wrong_price_is_regenerated_and_both_calls_are_counted(client, db):
    tid = _shop(db)
    model = ScriptedPlain("The black sneaker is ₦11,000.", "The black sneaker is ₦12,000.")
    with model.patch(), retrieval_patch(), automation_patch():
        r = send(client, tid, "how much is the black sneaker?")
    assert r.json()["message"]["content"] == "The black sneaker is ₦12,000."
    assert _meta(r)["offerings"]["guard"] == {
        "checked": True,
        "blocked": True,
        "regenerated": True,
        "fell_back": False,
    }
    assert "Correction: use only the prices" in model.prompts[1]
    assert "Correction" not in model.prompts[0]
    assert r.json()["message"]["token_count"] == 200


def test_two_bad_replies_fall_back_to_the_rows_and_offer_the_team(client, db):
    tid = _shop(db)
    model = ScriptedPlain("It is ₦11,000.", "I can do ₦10,000 for you.")
    with model.patch(), retrieval_patch(), automation_patch():
        r = send(client, tid, "how much is the black sneaker?")
    content = r.json()["message"]["content"]
    assert "₦12,000" in content and "11,000" not in content and "10,000" not in content
    assert _meta(r)["offerings"]["guard"]["fell_back"] is True
    assert _meta(r)["escalation_action"] == "offer_escalation"
    session = db.execute(select(ConversationSession)).scalars().all()[-1]
    assert (session.meta or {}).get("pending_escalation") is True


def test_haggling_below_listed_price_never_reaches_the_customer(client, db):
    tid = _shop(db)
    model = ScriptedPlain("Okay, deal at ₦8,000 for the black sneaker.", "Fine, ₦8,000 it is.")
    with model.patch(), retrieval_patch(), automation_patch():
        r = send(client, tid, "can you do ₦8,000 for the black sneaker?")
    assert "8,000" not in r.json()["message"]["content"]


def test_sales_mode_regenerates_with_the_same_call_and_keeps_structured_meta(client, db):
    tid = _shop(db, sales_enabled=True)
    model = ScriptedSales("The black sneaker is ₦11,000.", "The black sneaker is ₦12,000.")
    with model.patch(), retrieval_patch(), automation_patch():
        r = send(client, tid, "how much is the black sneaker?")
    assert r.json()["message"]["content"] == "The black sneaker is ₦12,000."
    assert len(model.calls) == 2
    assert "catalog_addendum" in model.calls[1]  # still the structured catalog call
    assert "Correction: use only the prices" in model.calls[1]["prompt"]
    assert _meta(r)["offerings"]["guard"]["regenerated"] is True


def test_strict_mode_after_lookup_failure_sends_no_price_at_all(client, db):
    from app.offerings import grounding

    tid = _shop(db)
    model = ScriptedPlain("The black sneaker is ₦12,000.", "It is ₦12,000.")
    with (
        patch.object(grounding, "_search", side_effect=RuntimeError("db down")),
        model.patch(),
        retrieval_patch(),
        automation_patch(),
    ):
        r = send(client, tid, "how much is the black sneaker?")
    assert r.status_code == 200
    assert "₦" not in r.json()["message"]["content"]
    assert _meta(r)["offerings"]["guard"]["fell_back"] is True


def test_tenant_without_items_has_no_offerings_meta_and_no_guard(client, db):
    tid = make_tenant(db)
    model = ScriptedPlain("Anything is ₦99,999 for you.")
    with model.patch(), retrieval_patch(), automation_patch():
        r = send(client, tid, "how much is the black sneaker?")
    assert "offerings" not in _meta(r)
    assert r.json()["message"]["content"] == "Anything is ₦99,999 for you."
    assert len(model.prompts) == 1


def test_knowledge_base_amount_is_allowed_in_its_own_sentence(client, db):
    tid = _shop(db)
    model = ScriptedPlain("The black sneaker is ₦12,000. Delivery within Lagos is ₦2,000.")

    async def fake_retrieve(self, *, tenant_id, query, max_chunks, score_threshold):
        from types import SimpleNamespace
        from uuid import uuid4

        chunk = SimpleNamespace(
            id=uuid4(),
            content="Delivery within Lagos costs ₦2,000 per order.",
            title="Delivery",
            source_type="text",
            metadata={},
        )
        return [chunk], [0.9]

    with (
        model.patch(),
        patch("app.services.rag_service.RAGService.retrieve_relevant_chunks", new=fake_retrieve),
        automation_patch(),
    ):
        r = send(client, tid, "how much is the black sneaker and delivery?")
    assert r.status_code == 200, r.text
    if _meta(r).get("offerings"):
        assert json.dumps(_meta(r)["offerings"]["guard"]["blocked"]) == "false"
