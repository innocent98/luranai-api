"""T6 review findings (Opus, read-only): overflow, bounds, pending offers, dropped lines, auto
handover, per-offer alerts, rollbacks, payloads, escaping. Written before the fixes."""

import json
from unittest.mock import patch
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DataError

from app.db.models.lead import Lead
from app.db.models.notification_log import NotificationLog, NotifEvent
from app.db.models.offering import LeadOfferingInterest, OfferingAvailability
from app.db.models.quote import Quote
from app.db.models.sales_agent_config import TenantSalesAgentConfig
from app.offerings.cart import catalog_quote, quote_too_large, resolve_cart, sync_interests
from app.offerings.facts import OfferingFacts
from tests.offerings_ai.helpers import add_offering, automation_patch, retrieval_patch, send
from tests.offerings_ai.test_cart import _lead, _seed, _sig
from tests.offerings_ai.test_cart_flow import (
    GOOD,
    Model,
    _cart,
    _lead as _the_lead,
    _logs,
    _meta,
    _run,
    _shop,
)

NEG_REPLY = "The owner is open to offers on the leather bag."


@pytest.fixture(scope="function")
def db(setup_test_database):
    """A private in-memory database per test with a plain session.

    The non-fatal handlers now call `db.rollback()`. The suite's shared fixture joins one outer
    transaction, where that would also erase the test's own setup, and SQLite cannot isolate a
    savepoint-style session, so these tests get their own throwaway database instead.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.db.base import Base

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture(scope="function")
def client(db):
    from fastapi.testclient import TestClient

    from app.db.session import get_db
    from app.main import app

    def override():
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


# -- 1. overflow ----------------------------------------------------------------------------


def test_a_total_beyond_the_quote_column_is_flagged_not_computed(db):
    tid, g = _seed(db)
    big = add_offering(db, tid, 20, "Generator", price=2_500_000)
    g.items.append(
        OfferingFacts(
            **{
                **g.items[0].__dict__,
                "id": big.id,
                "seq": 20,
                "name": "Generator",
                "price": 2_500_000,
            }
        )
    )
    lines = resolve_cart(db, tid, _sig([(20, 999)]), g).lines
    assert quote_too_large(lines) is True
    ok = resolve_cart(db, tid, _sig([(20, 10)]), g).lines
    assert quote_too_large(ok) is False and catalog_quote(ok, "NGN")["total"] == 25_000_000


def test_overflow_gives_no_quote_hands_over_and_the_reply_is_saved(client, db):
    tid = _shop(db)
    add_offering(db, tid, 20, "Generator", price=2_500_000)
    r = _run(
        client,
        db,
        tid,
        Model(("Noted: 999 generators.", _cart((20, 999)))),
        text="I want 999 generators",
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "quote" not in _meta(r)
    assert "too large for me to total" in body["message"]["content"]
    assert body["should_escalate"] is True and body["ticket_id"]
    assert db.execute(select(Quote).where(Quote.tenant_id == tid)).first() is None
    assert (
        db.execute(select(LeadOfferingInterest).where(LeadOfferingInterest.tenant_id == tid))
        .scalar_one()
        .qty
        == 999
    )


# -- rollbacks: a failure in any T6 step never costs the customer their reply -----------------


STEPS = [
    "app.api.v1.endpoints.chat.resolve_cart",
    "app.api.v1.endpoints.chat.record_cart",
    "app.api.v1.endpoints.chat.issue_quote",
    "app.api.v1.endpoints.chat.alert_owner",
    "app.offerings.turn.build_offerings_turn",
]


@pytest.mark.parametrize("target", STEPS)
def test_an_exception_in_any_step_still_saves_the_reply(client, db, target):
    tid = _shop(db)
    with patch(target, side_effect=RuntimeError("boom")):
        r = _run(client, db, tid, Model((GOOD, _cart((1, 2)))))
    assert r.status_code == 200, r.text
    assert "apologize" not in r.json()["message"]["content"]
    assert r.json()["message"]["id"]


def test_a_database_error_inside_the_quote_step_cannot_poison_the_session(client, db):
    tid = _shop(db)
    with patch(
        "app.services.quote_service.QuoteService.create_quote",
        side_effect=DataError("insert", {}, Exception("integer out of range")),
    ):
        r = _run(client, db, tid, Model((GOOD, _cart((1, 2)))))
    assert r.status_code == 200, r.text
    assert "apologize" not in r.json()["message"]["content"]
    assert (
        db.execute(select(LeadOfferingInterest).where(LeadOfferingInterest.tenant_id == tid))
        .scalar_one()
        .qty
        == 2
    )


# -- 2. offered price bounds -----------------------------------------------------------------


def test_only_a_figure_in_the_customers_current_message_can_be_an_offer(client, db):
    tid = _shop(db)
    offer = {"ref": "#3", "amount": 30000}
    r1 = _run(
        client,
        db,
        tid,
        Model((NEG_REPLY, _cart((3, 1)), offer)),
        text="₦30,000 for the leather bag?",
    )
    assert (
        db.execute(select(LeadOfferingInterest).where(LeadOfferingInterest.tenant_id == tid))
        .scalar_one()
        .offered_price
        == 30000
    )
    tid2 = _shop(db)
    r2 = _run(
        client, db, tid2, Model((NEG_REPLY, _cart((3, 1)), offer)), text="I want the leather bag"
    )
    row = db.execute(
        select(LeadOfferingInterest).where(LeadOfferingInterest.tenant_id == UUID(str(tid2)))
    ).scalar_one()
    assert row.offered_price is None and r1.status_code == r2.status_code == 200


# -- 3. a pending offer blocks a later quote -------------------------------------------------


def test_a_pending_offer_on_a_negotiable_item_blocks_the_quote_and_hands_over(db):
    import asyncio

    from app.offerings.orchestrate import issue_quote

    tid, g = _seed(db)
    lead = _lead(db, tid)
    sync_interests(
        db, lead, resolve_cart(db, tid, _sig([(3, 1)], {"ref": "#3", "amount": 30000}), g)
    )
    res = resolve_cart(db, tid, _sig([(3, 1)]), g)
    synced = sync_interests(db, lead, res)
    out = asyncio.run(issue_quote(db, lead, res, synced, "NGN"))
    assert out.quote is None and out.note == "A team member will confirm the price."
    assert out.handover is True
    assert db.execute(select(Quote).where(Quote.tenant_id == tid)).first() is None


def test_an_offer_on_a_fixed_price_item_never_blocks_the_quote(client, db):
    tid = _shop(db)
    offer = {"ref": "#1", "amount": 10000}
    r1 = _run(
        client,
        db,
        tid,
        Model(("ignored", _cart((1, 1)), offer)),
        text="₦10,000 for the black sneaker?",
    )
    assert "firm" in r1.json()["message"]["content"]
    assert _meta(r1)["quote"]["total"] == 12000  # at the listed price, never at the offer
    assert r1.json()["should_escalate"] is False
    sid = UUID(r1.json()["session_id"])
    r2 = _run(
        client,
        db,
        tid,
        Model(("Noted: one black sneaker.", _cart((1, 1)))),
        text="ok I'll take it",
        session_id=sid,
    )
    assert "A team member will confirm the price." not in r2.json()["message"]["content"]
    assert len(db.execute(select(Quote).where(Quote.tenant_id == tid)).scalars().all()) == 1


def test_an_offer_at_the_list_price_is_a_normal_cart(client, db):
    tid = _shop(db)
    r = _run(
        client,
        db,
        tid,
        Model(("Noted: the leather bag.", _cart((3, 1)), {"ref": "#3", "amount": 45000})),
        text="I'll pay ₦45,000 for the leather bag",
    )
    assert _meta(r)["quote"]["total"] == 45000
    assert r.json()["should_escalate"] is False
    row = db.execute(
        select(LeadOfferingInterest).where(LeadOfferingInterest.tenant_id == tid)
    ).scalar_one()
    assert row.offered_price is None


# -- 4. earlier items survive, dropped lines are explained ------------------------------------


def test_items_chosen_earlier_resolve_even_when_not_in_this_turns_block(client, db):
    tid = _shop(db)
    r1 = _run(client, db, tid, Model((GOOD, _cart((1, 2)))))
    sid = UUID(r1.json()["session_id"])
    r2 = _run(
        client,
        db,
        tid,
        Model(("Noted, plus the leather bag.", _cart((1, 2), (3, 1)))),
        text="add the leather bag",
        session_id=sid,
    )
    assert _meta(r2)["quote"]["total"] == 2 * 12000 + 45000
    assert {
        r.ref: r.qty
        for r in db.execute(
            select(LeadOfferingInterest).where(LeadOfferingInterest.tenant_id == tid)
        ).scalars()
    } == {
        "#1": 2,
        "#3": 1,
    }


def test_a_line_that_cannot_be_resolved_keeps_the_earlier_rows_and_says_so(client, db):
    tid = _shop(db)
    r1 = _run(client, db, tid, Model((GOOD, _cart((1, 2)))))
    sid = UUID(r1.json()["session_id"])
    r2 = _run(
        client,
        db,
        tid,
        Model(("Noted.", _cart((1, 2), (99, 1)))),
        text="and one more",
        session_id=sid,
    )  # #99 was never shown in this session
    content = r2.json()["message"]["content"]
    assert "#99" in content and "couldn't add" in content
    assert "quote" not in _meta(r2)
    assert {
        r.ref: r.qty
        for r in db.execute(
            select(LeadOfferingInterest).where(LeadOfferingInterest.tenant_id == tid)
        ).scalars()
    } == {"#1": 2}


def test_a_sold_out_line_is_named_and_blocks_the_quote(client, db):
    tid = _shop(db)
    add_offering(
        db, tid, 30, "Beach sandals", price=8000, availability="out_of_stock"
    )  # type: ignore[arg-type]
    r = _run(
        client,
        db,
        tid,
        Model(("Noted.", _cart((1, 1), (30, 1)))),
        text="the black sneaker and the beach sandals",
    )
    content = r.json()["message"]["content"]
    assert "Beach sandals: it is sold out" in content
    assert "quote" not in _meta(r)


# -- 5. offers hand over for real and alert per offer ----------------------------------------


def test_an_offer_escalates_for_real_and_the_ai_stops(client, db):
    tid = _shop(db)
    r = _run(
        client,
        db,
        tid,
        Model((NEG_REPLY, _cart((3, 1)), {"ref": "#3", "amount": 30000})),
        text="₦30,000 for the leather bag?",
    )
    body = r.json()
    assert body["should_escalate"] is True and body["ticket_id"]
    assert _meta(r)["escalation_action"] == "auto_escalate"
    assert "handing this to the team" in body["message"]["content"]


def test_an_offer_after_the_first_cart_still_reaches_the_owner(client, db):
    tid = _shop(db)
    r1 = _run(client, db, tid, Model((GOOD, _cart((1, 1)))))
    sid = UUID(r1.json()["session_id"])
    assert len(_logs(db, tid, NotifEvent.OFFERING_REQUEST)) == 1
    _run(
        client,
        db,
        tid,
        Model((NEG_REPLY, _cart((1, 1), (3, 1)), {"ref": "#3", "amount": 30000})),
        text="₦30,000 for the leather bag?",
        session_id=sid,
    )
    logs = _logs(db, tid, NotifEvent.OFFERING_REQUEST)
    assert len(logs) == 2
    offer = next(x for x in logs if x.entity_type == "offering_offer")
    assert "Leather bag" in offer.body and "₦45,000" in offer.body and "₦30,000" in offer.body


def test_an_offer_alert_is_keyed_by_lead_item_and_amount():
    from uuid import uuid4

    from app.offerings.orchestrate import offer_key

    lead, item = uuid4(), uuid4()
    assert offer_key(lead, item, 30000) == offer_key(lead, item, 30000)
    assert offer_key(lead, item, 30000) != offer_key(lead, item, 31000)
    assert offer_key(lead, item, 30000) != offer_key(uuid4(), item, 30000)


# -- 6. the score does not bounce --------------------------------------------------------------


def test_catalog_score_rows_persist_on_a_turn_without_a_cart(client, db):
    tid = _shop(db)
    r1 = _run(client, db, tid, Model((GOOD, _cart((1, 2)))))
    sid = UUID(r1.json()["session_id"])
    before = _the_lead(db, tid).lead_score
    _run(
        client,
        db,
        tid,
        Model(("The black sneaker is ₦12,000.", [])),
        text="yes that is right",
        session_id=sid,
    )
    lead = _the_lead(db, tid)
    assert lead.lead_score >= before
    assert "selected_items" in {b["key"] for b in lead.score_breakdown if b["fired"]}


# -- (a) the plan-tier quote never runs on a catalog cart ---------------------------------------


def test_the_tier_quote_is_off_on_a_catalog_cart(client, db):
    tid = _shop(db)
    cfg = db.execute(
        select(TenantSalesAgentConfig).where(TenantSalesAgentConfig.tenant_id == tid)
    ).scalar_one()
    cfg.pricing_tiers = [
        {
            "name": "Pro",
            "base_price": 30000,
            "min_users": 1,
            "max_users": 50,
            "per_seat_overage_price": 100,
        }
    ]
    db.commit()
    r = _run(
        client,
        db,
        tid,
        Model(("Noted: one phone repair.", _cart((4, 1)))),
        text="I'll take the phone repair",
    )
    assert "quote" not in _meta(r)


# -- (d)/(e) notification payloads -----------------------------------------------------------


def test_slack_text_is_escaped():
    """`_slack_body` escapes every value once (the owner's alert text included); the Block Kit
    wrapper adds no second layer."""
    from app.workers.sales_notifications import _slack_blocks, _slack_escape

    body = _slack_escape("Ada <!channel> & <http://x|click> offered")
    assert "<!channel>" not in body and "&lt;!channel&gt;" in body and "&amp;" in body
    text = _slack_blocks("T", body, "")["blocks"][1]["text"]["text"]
    assert text == body  # not escaped twice


def test_first_cart_alert_carries_prices_and_the_offer(client, db):
    tid = _shop(db)
    _run(
        client,
        db,
        tid,
        Model((NEG_REPLY, _cart((3, 1)), {"ref": "#3", "amount": 30000})),
        text="₦30,000 for the leather bag?",
    )
    first = next(x for x in _logs(db, tid, NotifEvent.OFFERING_REQUEST) if x.entity_type == "lead")
    assert "₦45,000" in first.body and "₦30,000" in first.body


def test_rep_email_amounts_carry_the_currency_symbol(client, db):
    from app.workers.sales_notifications import _entity_context

    tid = _shop(db)
    _run(client, db, tid, Model((GOOD, _cart((1, 2)))))
    log = next(x for x in _logs(db, tid, NotifEvent.OFFERING_REQUEST) if x.entity_type == "lead")
    assert _entity_context(db, log)["wanted"] == ["2 × Canvas sneaker black (listed ₦12,000)"]


def test_crm_payload_lists_the_wanted_items(client, db):
    from app.workers.sales_notifications import _crm_interests

    tid = _shop(db)
    _run(client, db, tid, Model((GOOD, _cart((1, 2)))))
    lead = _the_lead(db, tid)
    assert _crm_interests(db, lead) == [
        {
            "ref": "#1",
            "name": "Canvas sneaker black",
            "qty": 2,
            "price": 12000,
            "offered_price": None,
        }
    ]


# -- unit: sync flags -------------------------------------------------------------------------


def test_sync_reports_pending_and_changed_offers(db):
    tid, g = _seed(db)
    lead = _lead(db, tid)
    first = sync_interests(
        db, lead, resolve_cart(db, tid, _sig([(3, 1)], {"ref": "#3", "amount": 30000}), g)
    )
    assert first.offer_changed is True and first.pending_offer is True
    again = sync_interests(
        db, lead, resolve_cart(db, tid, _sig([(3, 1)], {"ref": "#3", "amount": 30000}), g)
    )
    assert again.offer_changed is False
    later = sync_interests(db, lead, resolve_cart(db, tid, _sig([(3, 1)]), g))
    assert later.pending_offer is True and later.has_offer is False


def test_unresolved_lines_are_reported_and_merge_instead_of_replacing(db):
    tid, g = _seed(db)
    lead = _lead(db, tid)
    sync_interests(db, lead, resolve_cart(db, tid, _sig([(1, 2)]), g))
    res = resolve_cart(db, tid, _sig([(2, 1), (99, 1)]), g)
    assert [(d.seq, d.reason) for d in res.dropped] == [(99, "unknown")]
    out = sync_interests(db, lead, res)
    assert out.partial is True
    rows = {
        r.ref: r.qty
        for r in db.execute(
            select(LeadOfferingInterest).where(LeadOfferingInterest.lead_id == lead.id)
        ).scalars()
    }
    assert rows == {"#1": 2, "#2": 1}


def test_an_offer_on_a_sold_out_item_is_dropped_and_named(db):
    tid, g = _seed(db)
    res = resolve_cart(db, tid, _sig(offered={"ref": "#5", "amount": 5000}), g)
    assert res.offered is None and [(d.seq, d.reason) for d in res.dropped] == [(5, "sold_out")]


def test_extra_seqs_let_earlier_choices_resolve(db):
    tid, g = _seed(db)
    g.items = [i for i in g.items if i.seq != 2]
    assert resolve_cart(db, tid, _sig([(2, 1)]), g).lines == []
    assert [
        l.facts.seq for l in resolve_cart(db, tid, _sig([(2, 1)]), g, extra_seqs={2}).lines
    ] == [2]


def test_the_tier_quote_stays_off_when_the_cart_step_failed_or_is_empty(client, db):
    from sqlalchemy import select as _select

    tid = _shop(db)
    cfg = db.execute(
        _select(TenantSalesAgentConfig).where(TenantSalesAgentConfig.tenant_id == tid)
    ).scalar_one()
    cfg.pricing_tiers = [
        {
            "name": "Pro",
            "base_price": 30000,
            "min_users": 1,
            "max_users": 50,
            "per_seat_overage_price": 100,
        }
    ]
    db.commit()
    with patch("app.api.v1.endpoints.chat.resolve_cart", side_effect=RuntimeError("boom")):
        r = _run(
            client,
            db,
            tid,
            Model(("Noted: one phone repair.", _cart((4, 1)))),
            text="I'll take the phone repair",
        )
    assert "quote" not in _meta(r)
