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

    def __init__(self, *steps, email=None):
        self.steps = list(steps)
        self.email = email

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
                        "email": self.email,
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
    assert meta["escalation_action"] == "auto_escalate"
    assert "quote" not in meta  # a pending counter-offer means a human prices it
    assert "handing this to the team" in r.json()["message"]["content"]
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
    assert _meta(r2)["escalation_action"] == "auto_escalate"


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
    assert (
        log.body
        == "Ada · 2 × Canvas sneaker black (listed ₦12,000); 1 × Leather bag (listed ₦45,000)"
    )


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
    log = next(x for x in _logs(db, tid, NotifEvent.OFFERING_REQUEST) if x.entity_type == "lead")
    ctx = _entity_context(db, log)
    assert ctx["kind"] == "offering_request"
    assert ctx["wanted"] == ["1 × Leather bag (listed ₦45,000), customer offered ₦30,000"]


def test_a_bargaining_turn_is_answered_by_code_and_the_models_text_is_dropped(client, db):
    tid = _shop(db)
    r = _run(
        client,
        db,
        tid,
        Model(
            (
                "Yes, the leather bag is ₦30,000 as you said. Deal!",
                _cart((3, 1)),
                {"ref": "#3", "amount": 30000},
            ),
        ),
        text="can you do ₦30,000 for the leather bag?",
    )
    content = r.json()["message"]["content"]
    assert content.startswith("I've noted your offer of ₦30,000 for the Leather bag.")
    assert "as you said" not in content and "Deal" not in content
    assert "handing this to the team" in content
    assert _meta(r)["offerings"]["guard"]["checked"] is False  # nothing to check: no model text
    assert r.json()["should_escalate"] is True


def test_a_firm_listed_price_is_stated_by_code_without_noting_an_offer(client, db):
    tid = _shop(db)
    r = _run(
        client,
        db,
        tid,
        Model(("The black sneaker is listed at ₦12,000.", None, {"ref": "#1", "amount": 10000})),
        text="can you do ₦10,000 for the black sneaker?",
    )
    content = r.json()["message"]["content"]
    assert content.endswith(
        "The listed price for the Canvas sneaker black is ₦12,000 and it is firm."
    )
    assert "noted your offer" not in content
    assert _meta(r)["escalation_action"] == "no_escalation"
    assert db.execute(select(LeadOfferingInterest)).scalar_one().offered_price == 10000


def test_an_offer_the_customer_did_not_make_is_ignored_and_a_huge_one_cannot_break_the_reply(
    client, db
):
    tid = _shop(db)
    r = _run(
        client,
        db,
        tid,
        Model(
            ("The owner is open to offers on the leather bag.", None, {"ref": "#3", "amount": 5})
        ),
        text="how much is the leather bag?",
    )
    assert r.status_code == 200 and db.execute(select(LeadOfferingInterest)).first() is None
    r2 = _run(
        client,
        db,
        tid,
        Model(
            (
                "The owner is open to offers on the leather bag.",
                _cart((3, 1)),
                {"ref": "#3", "amount": 10**30},
            )
        ),
        text="₦30,000 for the leather bag?",
    )
    assert r2.status_code == 200
    assert "apologize" not in r2.json()["message"]["content"]


def test_a_bare_number_offer_is_stored_and_handed_over_not_a_dead_end(client, db):
    tid = _shop(db)
    r = _run(
        client,
        db,
        tid,
        Model((NEG_REPLY, _cart((3, 1)), {"ref": "#3", "amount": 30000})),
        text="leather bag abeg 30000",
    )
    row = db.execute(select(LeadOfferingInterest)).scalar_one()
    assert (row.ref, row.offered_price) == ("#3", 30000)
    content = r.json()["message"]["content"]
    assert "noted your offer" in content and "handing this to the team" in content
    assert "Tell me the price you have in mind" not in content
    assert _meta(r)["escalation_action"] == "auto_escalate"


def test_on_voice_the_words_match_the_state_it_asks_it_does_not_claim_a_handover(client, db):
    from app.services.conversation_service import voice_channel

    tid = _shop(db)
    marker = voice_channel.set(True)
    try:
        r = _run(
            client,
            db,
            tid,
            Model((NEG_REPLY, _cart((3, 1)), {"ref": "#3", "amount": 30000})),
            text="leather bag abeg 30000",
        )
    finally:
        voice_channel.reset(marker)
    content = r.json()["message"]["content"]
    assert "noted your offer" in content
    assert "handing this to the team" not in content
    assert content.rstrip().endswith("Would you like me to connect you with the team?")
    assert _meta(r)["escalation_action"] == "offer_escalation"
    assert r.json()["should_escalate"] is False


def test_an_offer_keeps_the_agreement_rules_armed_for_the_next_replies(client, db):
    from app.offerings.turn import OFFER_WATCH_KEY, OFFER_WATCH_TURNS

    tid = _shop(db)
    r = _run(
        client,
        db,
        tid,
        Model((NEG_REPLY, _cart((3, 1)), {"ref": "#3", "amount": 30000})),
        text="leather bag abeg 30000",
    )
    sid = r.json()["session_id"]
    from app.db.models.conversation import ConversationSession

    from uuid import UUID

    row = db.get(ConversationSession, UUID(sid))
    db.refresh(row)
    assert (row.meta or {}).get(OFFER_WATCH_KEY) == OFFER_WATCH_TURNS


def test_next_offer_watch_counts_down_and_rearms():
    from app.offerings.turn import next_offer_watch

    assert next_offer_watch(0, True) == 5
    assert [next_offer_watch(n, False) for n in (5, 1, 0)] == [4, 0, 0]


def test_a_figure_only_offer_with_no_item_named_goes_to_a_human_not_the_model(client, db):
    tid = _shop(db)
    r = _run(
        client, db, tid, Model(("Sure, we can do that for you!", None, None)), text="I fit pay 18k"
    )
    content = r.json()["message"]["content"]
    assert "we can do that" not in content
    assert "team" in content


def test_a_bargaining_question_without_a_figure_does_not_arm_the_countdown():
    from app.offerings.turn import arms_countdown
    from tests.offerings_ai.test_guard import _grounding

    g = _grounding(matched=(11,))
    assert not arms_countdown("is the price negotiable?", g, False)
    assert arms_countdown("abeg 9000", g, False)
    assert arms_countdown("ok", g, True)  # the model returned a figure


def test_a_tenant_without_a_catalog_keeps_todays_lead_source_on_whatsapp(client, db):
    from tests.offerings_ai.helpers import make_tenant

    tid = make_tenant(db, sales_enabled=True, quotation_enabled=True)
    marker = message_channel.set("whatsapp")
    try:
        _run(client, db, tid, Model(("Hello! How can I help?", None)), text="hi, what do you do?")
    finally:
        message_channel.reset(marker)
    leads = db.execute(select(Lead).where(Lead.tenant_id == tid)).scalars().all()
    assert all(lead.source == "chat_widget" for lead in leads)


def test_an_offer_stops_blocking_quotes_once_a_human_has_joined_after_it(db):
    from datetime import UTC, datetime, timedelta

    from app.db.models.conversation import ConversationSession
    from app.offerings.cart import has_pending_offer

    tid = _shop(db)
    lead = Lead(tenant_id=tid, conversation_id=None, name="Pat", source="chat_widget")
    sess = ConversationSession(tenant_id=tid, user_identifier="u-pending")
    db.add_all([lead, sess])
    db.flush()
    lead.conversation_id = sess.id
    bag = db.execute(
        select(Offering).where(Offering.tenant_id == tid, Offering.seq == 3)
    ).scalar_one()
    db.add(
        LeadOfferingInterest(
            tenant_id=tid, lead_id=lead.id, offering_id=bag.id, ref="#3", name_snapshot="Leather bag",
            price_mode_snapshot="fixed", price_snapshot=45000, qty=1, offered_price=30000,
        )
    )  # fmt: skip
    db.flush()
    assert has_pending_offer(db, lead) is True
    sess.agent_joined_at = datetime.now(UTC) + timedelta(minutes=1)  # a human took it up after
    db.flush()
    assert has_pending_offer(db, lead) is False


def test_the_countdown_is_updated_from_the_value_read_under_the_lock(db):
    from app.db.models.conversation import ConversationSession
    from app.offerings.turn import OFFER_WATCH_KEY, offer_watch, remember_shown

    tid = _shop(db)
    sess = ConversationSession(tenant_id=tid, user_identifier="u-watch")
    db.add(sess)
    db.commit()
    remember_shown(db, sess.id, [], armed=True)
    db.refresh(sess)
    assert (sess.meta or {}).get(OFFER_WATCH_KEY) == 5
    remember_shown(db, sess.id, [], armed=False)  # a concurrent ordinary turn only runs it down
    db.refresh(sess)
    assert offer_watch(sess.meta) == 4
    remember_shown(db, sess.id, [], armed=None)  # a tenant without a catalog: untouched
    db.refresh(sess)
    assert offer_watch(sess.meta) == 4


def test_a_taken_up_offer_stays_resolved_when_the_customer_names_the_item_again(db):
    """Offer, a human joins, the customer re-selects the item twice: the quote goes out at list."""
    from datetime import UTC, datetime, timedelta

    from app.db.models.conversation import ConversationSession
    from app.offerings.cart import (
        CartResolution,
        ResolvedLine,
        ResolvedOffer,
        has_pending_offer,
        sync_interests,
    )
    from app.offerings.facts import facts_from_row

    tid = _shop(db)
    lead = Lead(tenant_id=tid, conversation_id=None, name="Pat", source="chat_widget")
    sess = ConversationSession(tenant_id=tid, user_identifier="u-resel")
    db.add_all([lead, sess])
    db.flush()
    lead.conversation_id = sess.id
    bag = db.execute(
        select(Offering).where(Offering.tenant_id == tid, Offering.seq == 3)
    ).scalar_one()
    facts = facts_from_row(bag)
    sync_interests(
        db,
        lead,
        CartResolution(lines=[ResolvedLine(facts, 1)], offered=ResolvedOffer(facts, 30000)),
        commit=False,
    )
    assert has_pending_offer(db, lead) is True
    sess.agent_joined_at = datetime.now(UTC) + timedelta(seconds=30)  # a human took it up
    db.flush()
    assert has_pending_offer(db, lead) is False
    for _ in range(2):  # "ok I'll take the leather bag", twice: delete and re-insert, offer carried
        sync_interests(db, lead, CartResolution(lines=[ResolvedLine(facts, 1)]), commit=False)
        assert has_pending_offer(db, lead) is False
    # a NEW or CHANGED figure is a new offer: pending again until the next human reply
    sync_interests(
        db,
        lead,
        CartResolution(lines=[ResolvedLine(facts, 1)], offered=ResolvedOffer(facts, 28000)),
        commit=False,
    )
    row = db.execute(
        select(LeadOfferingInterest).where(LeadOfferingInterest.lead_id == lead.id)
    ).scalar_one()
    assert row.offered_price == 28000


def test_a_catalog_quote_is_emailed_to_the_visitor_once_in_code_words(client, db):
    """A catalog quote takes the same visitor-email path as a plan-tier quote (#200): one queued
    send, the masked address in the reply, the model's own "I'll email it" removed, the quote card
    and the code-written lines unchanged."""
    from sqlalchemy import select as _select

    from app.db.models.notification_log import NotificationLog, NotifEvent
    from app.db.models.sales_agent_config import TenantSalesAgentConfig
    from tests.api.v1.test_chat_quote_visitor_email import _allow, _noop, _visitor_logs, jobs_seen

    jobs_seen.clear()
    tid = _shop(db)
    cfg = db.execute(
        _select(TenantSalesAgentConfig).where(TenantSalesAgentConfig.tenant_id == tid)
    ).scalar_one()
    cfg.notification_email = "rep@example.com"
    cfg.notify_channels = {"email": True}
    db.commit()
    reply = "Noted: 2 black sneakers. I'll email you the quote."
    model = Model((reply, _cart((1, 2))), email="alice@example.com")
    with (
        model.patch(),
        retrieval_patch(),
        automation_patch(),
        patch("app.services.notification_enqueue.enqueue_jobs_async", _noop),
        patch("app.services.quote_email_service.enforce_identifier_limit", _allow),
    ):
        r = send(client, tid, "I want two black sneakers, my email is alice@example.com")
    content = r.json()["message"]["content"]
    assert "I've sent the quote to a***@example.com" in content
    assert "alice@example.com" not in content and "email you the quote" not in content
    assert _meta(r)["quote"]["total"] == 24000  # the catalog quote card is unchanged
    (log,) = _visitor_logs(db, tid)
    assert log.recipient == "alice@example.com"
    assert [n for _id, n in jobs_seen if n == "send_visitor_quote_email"]
    # the owner's quote alert still fired for the catalog quote
    alerts = (
        db.execute(
            _select(NotificationLog).where(
                NotificationLog.tenant_id == tid,
                NotificationLog.event == NotifEvent.QUOTE_GENERATED,
            )
        )
        .scalars()
        .all()
    )
    assert alerts
