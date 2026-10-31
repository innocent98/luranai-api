"""T6 (money path): cart -> interests, catalog quote totals, offered price, text rendering.

Unit tests first (pure code on live rows), then the same rules through the endpoint.
"""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.db.models.lead import Lead
from app.db.models.offering import (
    LeadOfferingInterest,
    Offering,
    OfferingAvailability,
    OfferingPriceMode,
)
from app.db.models.quote import Quote
from app.offerings.cart import catalog_quote, resolve_cart, sync_interests
from app.offerings.facts import OfferingFacts, facts_from_row
from app.offerings.grounding import GroundingResult
from app.offerings.quote_text import render_quote_text
from tests.offerings_ai.helpers import add_offering, make_tenant


def _seed(db):
    tid = make_tenant(db, sales_enabled=True, quotation_enabled=True)
    rows = {
        1: add_offering(db, tid, 1, "Canvas sneaker black", price=12000),
        2: add_offering(db, tid, 2, "Canvas sneaker white", price=13500),
        3: add_offering(db, tid, 3, "Leather bag", price=45000, negotiable=True),
        4: add_offering(
            db,
            tid,
            4,
            "Phone repair",
            price=5000,
            price_mode=OfferingPriceMode.FROM,
            price_note="per phone",
        ),
        5: add_offering(
            db,
            tid,
            5,
            "Sold out sandals",
            price=8000,
            availability=OfferingAvailability.OUT_OF_STOCK,
        ),
        6: add_offering(db, tid, 6, "Custom portrait", price_mode=OfferingPriceMode.ON_REQUEST),
    }
    seqs = list(rows)
    facts = [facts_from_row(db.get(Offering, rows[s].id)) for s in seqs]
    return tid, GroundingResult(mode="detail", block="", items=facts, matched_seqs=seqs)


def _lead(db, tid):
    lead = Lead(
        tenant_id=tid, conversation_id=uuid4(), intent="PURCHASE_INTENT", source="chat_widget"
    )
    db.add(lead)
    db.commit()
    return lead


def _sig(cart=(), offered=None):
    return {
        "catalog_refs": [],
        "cart": [{"ref": f"#{s}", "qty": q} for s, q in cart],
        "offered_price": offered,
    }


# -- resolve_cart ---------------------------------------------------------------------------


def test_cart_lines_are_read_live_from_the_database(db):
    tid, g = _seed(db)
    db.get(Offering, g.items[0].id).price = 15000  # changed after the block was built
    db.commit()
    res = resolve_cart(db, tid, _sig([(1, 2)]), g)
    assert [(l.facts.seq, l.qty, l.facts.price) for l in res.lines] == [(1, 2, 15000)]


def test_refs_not_injected_this_turn_are_dropped(db):
    tid, g = _seed(db)
    g.items = [i for i in g.items if i.seq != 2]
    res = resolve_cart(db, tid, _sig([(1, 1), (2, 1), (99, 1)]), g)
    assert [l.facts.seq for l in res.lines] == [1]


def test_sold_out_archived_and_deleted_items_are_dropped(db):
    tid, g = _seed(db)
    db.get(Offering, g.items[1].id).status = "archived"
    db.commit()
    res = resolve_cart(db, tid, _sig([(1, 1), (2, 1), (5, 1)]), g)
    assert [l.facts.seq for l in res.lines] == [1]


def test_another_tenants_item_with_the_same_seq_is_never_resolved(db):
    tid, g = _seed(db)
    other = make_tenant(db)
    add_offering(db, other, 7, "Foreign thing", price=1)
    g.items.append(
        OfferingFacts(**{**g.items[0].__dict__, "id": uuid4(), "seq": 7, "name": "Injected lie"})
    )
    assert resolve_cart(db, tid, _sig([(7, 1)]), g).lines == []


def test_quantities_merge_and_garbage_is_dropped(db):
    tid, g = _seed(db)
    res = resolve_cart(db, tid, _sig([(1, 1), (1, 2), (2, 0)]), g)
    assert [(l.facts.seq, l.qty) for l in res.lines] == [(1, 3)]


def test_offered_price_needs_a_live_injected_item(db):
    tid, g = _seed(db)
    assert (
        resolve_cart(db, tid, _sig(offered={"ref": "#3", "amount": 30000}), g).offered.amount
        == 30000
    )
    assert resolve_cart(db, tid, _sig(offered={"ref": "#99", "amount": 30000}), g).offered is None
    assert resolve_cart(db, tid, _sig(offered={"ref": "#3", "amount": 0}), g).offered is None


# -- the catalog quote (money) --------------------------------------------------------------


def _lines(db, tid, g, cart):
    return resolve_cart(db, tid, _sig(cart), g).lines


def test_quote_total_is_the_sum_of_qty_times_live_price(db):
    tid, g = _seed(db)
    quote = catalog_quote(_lines(db, tid, g, [(1, 2), (2, 1), (3, 3)]), "NGN")
    assert quote == {
        "currency": "NGN",
        "line_items": [
            {"label": "Canvas sneaker black", "detail": "2 × ₦12,000", "amount": 24000},
            {"label": "Canvas sneaker white", "detail": "1 × ₦13,500", "amount": 13500},
            {"label": "Leather bag", "detail": "3 × ₦45,000", "amount": 135000},
        ],
        "total": 172500,
    }
    assert quote["total"] == sum(li["amount"] for li in quote["line_items"])


@pytest.mark.parametrize("extra", [4, 6])
def test_a_from_or_on_request_line_means_no_quote(db, extra):
    tid, g = _seed(db)
    assert catalog_quote(_lines(db, tid, g, [(1, 1), (extra, 1)]), "NGN") is None


def test_empty_cart_means_no_quote(db):
    assert catalog_quote([], "NGN") is None


def test_quote_uses_the_currency_symbol_of_the_tenant(db):
    tid, g = _seed(db)
    assert (
        catalog_quote(_lines(db, tid, g, [(1, 1)]), "USD")["line_items"][0]["detail"]
        == "1 × $12,000"
    )


def test_quote_money_is_integer_even_for_big_quantities(db):
    tid, g = _seed(db)
    q = catalog_quote(_lines(db, tid, g, [(3, 999)]), "NGN")
    assert q["total"] == 999 * 45000 and isinstance(q["total"], int)


def test_service_price_note_appears_in_the_detail(db):
    tid, _g = _seed(db)
    row = add_offering(db, tid, 9, "Haircut", price=3000, price_note="per cut")
    g = GroundingResult(
        mode="detail", block="", items=[facts_from_row(db.get(Offering, row.id))], matched_seqs=[9]
    )
    q = catalog_quote(resolve_cart(db, tid, _sig([(9, 2)]), g).lines, "NGN")
    assert q["line_items"][0]["detail"] == "2 × ₦3,000 per cut"


# -- text rendering for channels without cards ----------------------------------------------


def test_render_quote_text_comes_only_from_the_quote():
    quote = {
        "currency": "NGN",
        "line_items": [
            {"label": "Canvas sneaker black", "detail": "2 × ₦12,000", "amount": 24000},
            {"label": "Leather bag", "detail": "1 × ₦45,000", "amount": 45000},
        ],
        "total": 69000,
    }
    assert render_quote_text(quote) == (
        "2 × Canvas sneaker black — ₦24,000\n"
        "1 × Leather bag — ₦45,000\n"
        "Total: ₦69,000\n"
        "The team will confirm availability and arrange payment."
    )


def test_render_quote_text_survives_an_unexpected_detail():
    quote = {
        "currency": "NGN",
        "line_items": [{"label": "X", "detail": "weird", "amount": 5}],
        "total": 5,
    }
    assert "X — ₦5" in render_quote_text(quote)


# -- interests ------------------------------------------------------------------------------


def _interests(db, lead):
    return {
        r.ref: r
        for r in db.execute(
            select(LeadOfferingInterest).where(LeadOfferingInterest.lead_id == lead.id)
        ).scalars()
    }


def test_interests_store_snapshots(db):
    tid, g = _seed(db)
    lead = _lead(db, tid)
    out = sync_interests(db, lead, resolve_cart(db, tid, _sig([(1, 2), (4, 1)]), g))
    rows = _interests(db, lead)
    assert out.first_cart is True
    assert (
        rows["#1"].name_snapshot,
        rows["#1"].price_mode_snapshot,
        rows["#1"].price_snapshot,
        rows["#1"].qty,
    ) == ("Canvas sneaker black", "fixed", 12000, 2)
    assert (rows["#4"].price_mode_snapshot, rows["#4"].price_note_snapshot) == ("from", "per phone")
    assert rows["#1"].offered_price is None and rows["#1"].tenant_id == tid


def test_a_later_cart_replaces_the_selection_and_keeps_prior_offers(db):
    tid, g = _seed(db)
    lead = _lead(db, tid)
    sync_interests(
        db, lead, resolve_cart(db, tid, _sig([(3, 1)], {"ref": "#3", "amount": 30000}), g)
    )
    out = sync_interests(db, lead, resolve_cart(db, tid, _sig([(3, 2), (1, 1)]), g))
    rows = _interests(db, lead)
    assert out.first_cart is False
    assert set(rows) == {"#1", "#3"} and rows["#3"].qty == 2 and rows["#3"].offered_price == 30000


def test_an_empty_cart_never_clears_the_selection(db):
    tid, g = _seed(db)
    lead = _lead(db, tid)
    sync_interests(db, lead, resolve_cart(db, tid, _sig([(1, 1)]), g))
    sync_interests(db, lead, resolve_cart(db, tid, _sig(), g))
    assert set(_interests(db, lead)) == {"#1"}


def test_an_offer_creates_the_interest_row_when_the_item_is_not_in_the_cart(db):
    tid, g = _seed(db)
    lead = _lead(db, tid)
    sync_interests(db, lead, resolve_cart(db, tid, _sig(offered={"ref": "#3", "amount": 30000}), g))
    row = _interests(db, lead)["#3"]
    assert (row.qty, row.offered_price) == (1, 30000)


def test_snapshots_survive_item_deletion(db):
    tid, g = _seed(db)
    lead = _lead(db, tid)
    sync_interests(db, lead, resolve_cart(db, tid, _sig([(1, 1)]), g))
    db.delete(db.get(Offering, g.items[0].id))
    db.commit()
    row = _interests(db, lead)["#1"]
    assert row.name_snapshot == "Canvas sneaker black" and row.price_snapshot == 12000


@pytest.mark.parametrize(
    "seq, amount, again, handover",
    [
        (3, 30000, False, True),  # negotiable, below listed: hand over
        (3, 45000, False, False),  # at the listed price: nothing to negotiate
        (3, 50000, False, False),  # above it
        (1, 10000, False, False),  # not negotiable, first ask: the listed price stands
        (1, 10000, True, True),  # not negotiable but the customer insists
    ],
)
def test_handover_rules_for_offered_prices(db, seq, amount, again, handover):
    tid, g = _seed(db)
    lead = _lead(db, tid)
    offer = {"ref": f"#{seq}", "amount": amount}
    if again:
        sync_interests(
            db, lead, resolve_cart(db, tid, _sig(offered={"ref": f"#{seq}", "amount": amount}), g)
        )
    out = sync_interests(db, lead, resolve_cart(db, tid, _sig(offered=offer), g))
    assert out.handover is handover
    assert out.has_offer is True


def test_an_offer_the_customer_never_wrote_is_not_stored(db):
    tid, g = _seed(db)
    sig = _sig(offered={"ref": "#3", "amount": 30000})
    assert resolve_cart(db, tid, sig, g, customer_amounts={30000}).offered.amount == 30000
    assert resolve_cart(db, tid, sig, g, customer_amounts={25000}).offered is None
    assert resolve_cart(db, tid, sig, g, customer_amounts=set()).offered is None
