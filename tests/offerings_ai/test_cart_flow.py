"""T6 through the endpoint: cart -> lead + interests -> quote card -> owner alert -> handover."""

from unittest.mock import patch
from uuid import UUID

import pytest
from sqlalchemy import select

from app.db.models.lead import Lead
from app.db.models.notification_log import NotificationLog, NotifEvent
from app.db.models.offering import (
    LeadOfferingInterest,
    Offering,
    OfferingPriceMode,
)
from app.db.models.quote import Quote
from app.services.conversation_service import message_channel
from tests.offerings_ai.helpers import (
    add_offering,
    automation_patch,
    make_tenant,
    retrieval_patch,
    send,
)


def _shop(db, **kw):
    kw.setdefault("sales_enabled", True)
    kw.setdefault("quotation_enabled", True)
    tid = make_tenant(db, **kw)
    add_offering(db, tid, 1, "Canvas sneaker black", price=12000, category="Sneakers")
    add_offering(db, tid, 2, "Canvas sneaker white", price=13500, category="Sneakers")
    add_offering(db, tid, 3, "Leather bag", price=45000, negotiable=True)
    add_offering(db, tid, 4, "Phone repair", price=5000, price_mode=OfferingPriceMode.FROM)
    return tid


class Model:
    """Scripted structured sales model: each step is (reply, cart, offered_price, side_effect)."""

    def __init__(self, *steps):
        self.steps = list(steps)

    def patch(self):
        async def fake(_self, prompt, config, **kwargs):
            reply, cart, offered, effect = (list(self.steps.pop(0)) + [None] * 4)[:4]
            if effect:
                effect()
            signals = {
                "asked_features": False,
                "requested_demo": False,
                "mentioned_competitor": False,
                "ready_to_buy": True,
                "offer_meeting": False,
                "offer_quote": False,
                "catalog_refs": [f"#{r['ref'][1:]}" for r in (cart or [])],
                "cart": cart or [],
                "offered_price": offered,
            }
            return (
                reply,
                {
                    "intent": "PURCHASE_INTENT",
                    "captured": {
                        "name": "Ada",
                        "email": None,
                        "company": None,
                        "team_size": None,
                        "industry": None,
                        "plan_interest": None,
                    },
                    "signals": signals,
                },
                100,
                False,
            )

        return patch("app.services.rag_service.RAGService.generate_sales_response", new=fake)


def _run(client, db, tid, model, text="I want two black sneakers", session_id=None):
    with model.patch(), retrieval_patch(), automation_patch():
        return send(client, tid, text, session_id=session_id)


def _cart(*pairs):
    return [{"ref": f"#{s}", "qty": q} for s, q in pairs]


def _meta(r):
    return r.json()["message"]["meta"]


def _lead(db, tid):
    return db.execute(select(Lead).where(Lead.tenant_id == tid)).scalar_one()


def _logs(db, tid, event):
    return (
        db.execute(
            select(NotificationLog).where(
                NotificationLog.tenant_id == tid, NotificationLog.event == event
            )
        )
        .scalars()
        .all()
    )


GOOD = "Noted: two Canvas sneaker black. What name should I put down?"


def test_cart_creates_lead_interests_quote_and_alerts(client, db):
    tid = _shop(db)
    r = _run(client, db, tid, Model((GOOD, _cart((1, 2), (2, 1)))))
    assert r.status_code == 200, r.text
    meta = _meta(r)
    assert meta["quote"] == {
        "currency": "NGN",
        "line_items": [
            {"label": "Canvas sneaker black", "detail": "2 × ₦12,000", "amount": 24000},
            {"label": "Canvas sneaker white", "detail": "1 × ₦13,500", "amount": 13500},
        ],
        "total": 37500,
    }
    lead = _lead(db, tid)
    rows = (
        db.execute(select(LeadOfferingInterest).where(LeadOfferingInterest.lead_id == lead.id))
        .scalars()
        .all()
    )
    assert sorted((x.ref, x.qty, x.price_snapshot) for x in rows) == [
        ("#1", 2, 12000),
        ("#2", 1, 13500),
    ]
    quote = db.execute(select(Quote).where(Quote.tenant_id == tid)).scalar_one()
    assert quote.total == 37500 and quote.line_items == meta["quote"]["line_items"]
    assert len(_logs(db, tid, NotifEvent.QUOTE_GENERATED)) == 1
    assert len(_logs(db, tid, NotifEvent.OFFERING_REQUEST)) == 1
    assert lead.source == "chat_widget"


def test_quote_total_uses_rows_read_after_the_reply(client, db):
    tid = _shop(db)

    def reprice():
        row = db.execute(
            select(Offering).where(Offering.tenant_id == tid, Offering.seq == 1)
        ).scalar_one()
        row.price = 15000
        db.commit()

    r = _run(client, db, tid, Model((GOOD, _cart((1, 2)), None, reprice)))
    assert _meta(r)["quote"]["total"] == 30000
    assert db.execute(select(Quote)).scalar_one().total == 30000


@pytest.mark.parametrize("extra", [4])
def test_a_from_priced_line_gives_interests_but_no_quote(client, db, extra):
    tid = _shop(db)
    r = _run(
        client, db, tid, Model(("Noted. What name should I put down?", _cart((1, 1), (extra, 1))))
    )
    assert "quote" not in _meta(r)
    assert db.execute(select(Quote)).first() is None
    assert len(db.execute(select(LeadOfferingInterest)).scalars().all()) == 2
    assert len(_logs(db, tid, NotifEvent.OFFERING_REQUEST)) == 1
    assert _logs(db, tid, NotifEvent.QUOTE_GENERATED) == []


def test_lite_plan_gets_interests_and_no_quote(client, db):
    tid = _shop(db, sales_close=False)
    r = _run(client, db, tid, Model((GOOD, _cart((1, 2)))))
    assert "quote" not in _meta(r)
    assert db.execute(select(Quote)).first() is None
    assert len(db.execute(select(LeadOfferingInterest)).scalars().all()) == 1


def test_quotation_switched_off_gets_no_quote(client, db):
    tid = _shop(db, quotation_enabled=False)
    r = _run(client, db, tid, Model((GOOD, _cart((1, 2)))))
    assert "quote" not in _meta(r)
    assert len(db.execute(select(LeadOfferingInterest)).scalars().all()) == 1


def test_the_same_cart_next_turn_makes_no_second_quote_or_alert(client, db):
    tid = _shop(db)
    r1 = _run(client, db, tid, Model((GOOD, _cart((1, 2)))))
    sid = UUID(r1.json()["session_id"])
    r2 = _run(
        client, db, tid, Model((GOOD, _cart((1, 2)))), text="yes two black ones", session_id=sid
    )
    assert "quote" not in _meta(r2)
    assert len(db.execute(select(Quote)).scalars().all()) == 1
    assert len(_logs(db, tid, NotifEvent.OFFERING_REQUEST)) == 1


def test_a_changed_cart_makes_a_new_quote_but_no_second_owner_alert(client, db):
    tid = _shop(db)
    r1 = _run(client, db, tid, Model((GOOD, _cart((1, 2)))))
    sid = UUID(r1.json()["session_id"])
    r2 = _run(client, db, tid, Model((GOOD, _cart((1, 3)))), text="make it three", session_id=sid)
    assert _meta(r2)["quote"]["total"] == 36000
    assert len(db.execute(select(Quote)).scalars().all()) == 2
    assert len(_logs(db, tid, NotifEvent.OFFERING_REQUEST)) == 1


def test_cart_refs_not_in_the_block_are_ignored(client, db):
    tid = _shop(db)
    r = _run(client, db, tid, Model((GOOD, _cart((99, 1)))))
    assert db.execute(select(LeadOfferingInterest)).first() is None
    assert "quote" not in _meta(r)


def test_cart_never_leaks_across_tenants(client, db):
    tid = _shop(db)
    other = _shop(db)
    r = _run(client, db, tid, Model((GOOD, _cart((1, 1)))))
    assert r.status_code == 200
    assert (
        db.execute(
            select(LeadOfferingInterest).where(LeadOfferingInterest.tenant_id == other)
        ).first()
        is None
    )


def test_support_mode_tenant_records_nothing(client, db):
    tid = _shop(db, sales_enabled=False)

    async def plain(
        _self, prompt, max_tokens=800, model_name=None, system_message=None, image_urls=None
    ):
        return "Noted: the Canvas sneaker black is ₦12,000.", 50, False

    with (
        patch("app.services.rag_service.RAGService.generate_ai_response", new=plain),
        retrieval_patch(),
        automation_patch(),
    ):
        r = send(client, tid, "I want two black sneakers")
    assert r.status_code == 200
    assert db.execute(select(Lead)).first() is None
    assert db.execute(select(LeadOfferingInterest)).first() is None
    assert _logs(db, tid, NotifEvent.OFFERING_REQUEST) == []


# -- offered prices (D1) --------------------------------------------------------------------


NEG_REPLY = "The owner is open to offers on the leather bag. I've noted your offer; the team will follow up."


def test_offer_on_a_negotiable_item_is_stored_and_handed_over(client, db):
    tid = _shop(db)
    r = _run(
        client,
        db,
        tid,
        Model((NEG_REPLY, _cart((3, 1)), {"ref": "#3", "amount": 30000})),
        text="can you do ₦30,000 for the leather bag?",
    )
    row = db.execute(select(LeadOfferingInterest)).scalar_one()
    assert (row.ref, row.offered_price, row.price_snapshot) == ("#3", 30000, 45000)
    meta = _meta(r)
    assert meta["escalation_action"] == "offer_escalation"
    assert "quote" not in meta  # a pending counter-offer means a human prices it
    assert "connect you with the team" in r.json()["message"]["content"]
    assert "30,000" not in "".join(
        li["detail"] for li in (meta.get("quote") or {}).get("line_items", [])
    )


def test_offer_on_a_fixed_item_first_time_keeps_the_listed_price_standing(client, db):
    tid = _shop(db)
    r = _run(
        client,
        db,
        tid,
        Model(
            (
                "The black sneaker is listed at ₦12,000. I've noted your offer.",
                None,
                {"ref": "#1", "amount": 10000},
            )
        ),
        text="can you do ₦10,000 for the black sneaker?",
    )
    assert db.execute(select(LeadOfferingInterest)).scalar_one().offered_price == 10000
    assert _meta(r)["escalation_action"] == "no_escalation"


def test_insisting_on_the_same_item_hands_over(client, db):
    tid = _shop(db)
    offer = {"ref": "#1", "amount": 10000}
    reply = "The black sneaker is listed at ₦12,000. I've noted your offer."
    r1 = _run(
        client,
        db,
        tid,
        Model((reply, None, offer)),
        text="can you do ₦10,000 for the black sneaker?",
    )
    sid = UUID(r1.json()["session_id"])
    r2 = _run(
        client, db, tid, Model((reply, None, offer)), text="please, ₦10,000 only", session_id=sid
    )
    assert _meta(r2)["escalation_action"] == "offer_escalation"


# -- lead source, scoring --------------------------------------------------------------------


@pytest.mark.parametrize("channel, expected", [("whatsapp", "whatsapp"), (None, "chat_widget")])
def test_lead_source_follows_the_channel(client, db, channel, expected):
    tid = _shop(db)
    marker = message_channel.set(channel)
    try:
        _run(client, db, tid, Model((GOOD, _cart((1, 2)))))
    finally:
        message_channel.reset(marker)
    assert _lead(db, tid).source == expected


def test_lead_source_for_voice_and_hosted_page():
    from types import SimpleNamespace

    from app.db.models.visitor import VisitorSource
    from app.offerings.turn import lead_source

    page = SimpleNamespace(created_from=VisitorSource.SUPPORT_PAGE)
    widget = SimpleNamespace(created_from=VisitorSource.WIDGET)
    assert lead_source("voice", widget) == "voice"
    assert lead_source("whatsapp", None) == "whatsapp"
    assert lead_source(None, page) == "hosted_page"
    assert lead_source(None, widget) == "chat_widget"
    assert lead_source(None, None) == "chat_widget"


def test_selected_items_and_offer_show_in_the_score_breakdown(client, db):
    tid = _shop(db)
    _run(
        client,
        db,
        tid,
        Model((NEG_REPLY, _cart((3, 1)), {"ref": "#3", "amount": 30000})),
        text="₦30,000 for the bag?",
    )
    fired = {b["key"] for b in _lead(db, tid).score_breakdown if b["fired"]}
    assert {"selected_items", "price_offer"} <= fired


def test_leads_without_catalog_activity_keep_the_nine_signal_breakdown(client, db):
    tid = _shop(db)
    _run(client, db, tid, Model(("Which one would you like?", [])), text="what do you have?")
    assert len(_lead(db, tid).score_breakdown) == 9


def test_an_invalid_model_offer_does_not_touch_the_score(client, db):
    tid = _shop(db)
    _run(
        client,
        db,
        tid,
        Model(("Which one would you like?", [], {"ref": "#99", "amount": 5})),
        text="what do you have?",
    )
    keys = {b["key"] for b in _lead(db, tid).score_breakdown}
    assert len(keys) == 9 and "price_offer" not in keys


def test_owner_alert_names_the_items(client, db):
    tid = _shop(db)
    _run(client, db, tid, Model((GOOD, _cart((1, 2), (3, 1)))))
    log = _logs(db, tid, NotifEvent.OFFERING_REQUEST)[0]
    assert log.title == "New item request"
    assert log.body == "Ada · 2 × Canvas sneaker black, 1 × Leather bag"


def test_rep_email_context_lists_items_with_listed_and_offered_prices(client, db):
    from app.workers.sales_notifications import _entity_context

    tid = _shop(db)
    _run(
        client,
        db,
        tid,
        Model((NEG_REPLY, _cart((3, 1)), {"ref": "#3", "amount": 30000})),
        text="₦30,000 for the bag?",
    )
    log = _logs(db, tid, NotifEvent.OFFERING_REQUEST)[0]
    ctx = _entity_context(db, log)
    assert ctx["kind"] == "offering_request"
    assert ctx["items"] == ["1 × Leather bag (listed 45,000), customer offered 30,000"]


def test_the_offer_line_is_written_by_code_and_the_model_may_not_repeat_the_figure(client, db):
    tid = _shop(db)
    r = _run(
        client,
        db,
        tid,
        Model((NEG_REPLY, _cart((3, 1)), {"ref": "#3", "amount": 30000})),
        text="can you do ₦30,000 for the leather bag?",
    )
    content = r.json()["message"]["content"]
    assert content.startswith(NEG_REPLY)
    assert "I've noted your offer of ₦30,000 for the Leather bag." in content
    # a model that restates the customer's figure beside the item is stopped by the guard
    r2 = _run(
        client,
        db,
        tid,
        Model(
            ("Yes, the leather bag is ₦30,000 as you said.", _cart((3, 1)), None),
            ("The owner is open to offers on the leather bag.", _cart((3, 1)), None),
        ),
        text="₦30,000 for the leather bag?",
    )
    assert "30,000" not in r2.json()["message"]["content"]
    assert _meta(r2)["offerings"]["guard"]["blocked"] is True
