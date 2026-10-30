"""Import preview/apply: nothing saved on preview, atomic apply, upsert rules, cap, limits."""

from datetime import UTC, datetime, timedelta

import pytest

from app.db.models.offering import (
    Offering,
    OfferingActivityDay,
    OfferingStatus,
    TenantOfferingsSettings,
)
from app.db.models.user import UserRole
from tests.entitlements.helpers import make_tenant, make_user
from tests.offerings.helpers import URL, add_items, body

PREVIEW = f"{URL}/import/preview"
APPLY = f"{URL}/import"
CSV = "name,kind,price,availability,category\nCanvas sneaker,product,12000,in stock,Shoes\nHaircut,service,5k,limited,Services\n"


@pytest.fixture
def shop(db_session):
    tenant = make_tenant(db_session, "professional")
    _, headers = make_user(db_session, tenant)
    return tenant, headers


def _items(db, tenant, status=OfferingStatus.ACTIVE):
    db.expire_all()
    return (
        db.query(Offering)
        .filter_by(tenant_id=tenant.id, status=status)
        .order_by(Offering.seq)
        .all()
    )


def _post(client, headers, url, content, fmt="csv", mode="add", **kw):
    return client.post(
        url, json={"format": fmt, "content": content, "mode": mode, **kw}, headers=headers
    )


# --- preview --------------------------------------------------------------------------------


def test_preview_describes_every_row_and_saves_nothing(pg_client, db_session, shop):
    tenant, headers = shop
    content = CSV + "Bad price,product,abc,in stock,Shoes\n"
    r = _post(pg_client, headers, PREVIEW, content)
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["summary"] == {"create": 2, "update": 0, "error": 1, "over_limit_by": 0}
    first, second, third = data["rows"]
    assert first == {
        "row": 1,
        "status": "create",
        "parsed": {
            "kind": "product",
            "name": "Canvas sneaker",
            "price_mode": "fixed",
            "price": 12000,
            "availability": "in_stock",
            "category": "Shoes",
            "description": None,
            "keywords": None,
            "price_note": None,
            "quantity": None,
            "negotiable": False,
            "external_ref": None,
        },
        "errors": [],
    }
    assert second["parsed"]["price"] == 5000 and second["parsed"]["availability"] == "limited"
    assert third["status"] == "error" and third["errors"] == [
        {"field": "price", "code": "price_invalid"}
    ]
    assert _items(db_session, tenant) == []
    assert db_session.query(TenantOfferingsSettings).filter_by(tenant_id=tenant.id).count() == 0
    assert db_session.query(OfferingActivityDay).filter_by(tenant_id=tenant.id).count() == 0


def test_preview_lines_format(pg_client, shop):
    _, headers = shop
    r = _post(
        pg_client, headers, PREVIEW, "Canvas sneaker - 12000 - in stock\nHaircut\n", fmt="lines"
    )
    rows = r.json()["data"]["rows"]
    assert [x["status"] for x in rows] == ["create", "create"]
    assert rows[1]["parsed"]["price_mode"] == "on_request" and rows[1]["parsed"]["price"] is None


def test_defaults_for_missing_columns(pg_client, shop):
    _, headers = shop
    parsed = _post(pg_client, headers, PREVIEW, "name,price\nSneaker,900\nConsult,\n").json()[
        "data"
    ]["rows"]
    assert (
        parsed[0]["parsed"]["kind"] == "product"
        and parsed[0]["parsed"]["availability"] == "in_stock"
    )
    assert parsed[0]["parsed"]["price_mode"] == "fixed"
    assert (
        parsed[1]["parsed"]["price_mode"] == "on_request" and parsed[1]["parsed"]["price"] is None
    )


def test_price_from_prefix_and_value_aliases(pg_client, shop):
    _, headers = shop
    content = "name,price,availability,negotiable,quantity\nA,from 3k,sold out,yes,5\nB,500,Available,no,\n"
    rows = _post(pg_client, headers, PREVIEW, content).json()["data"]["rows"]
    a, b = rows[0]["parsed"], rows[1]["parsed"]
    assert (a["price_mode"], a["price"], a["availability"], a["negotiable"], a["quantity"]) == (
        "from",
        3000,
        "out_of_stock",
        True,
        5,
    )
    assert (b["availability"], b["negotiable"]) == ("in_stock", False)


@pytest.mark.parametrize(
    "content,field,code",
    [
        ("name,price_mode,price\nA,fixed,\n", "price", "price_required"),
        ("name,price_mode,price\nA,on_request,500\n", "price", "price_not_allowed"),
        ("name,price_mode,price\nA,fixed,0\n", "price", "price_out_of_range"),
        ("name,price,availability\nA,500,maybe\n", "availability", "availability_invalid"),
        ("name,price,kind\nA,500,gadget\n", "kind", "kind_invalid"),
        ("name,price,price_mode\nA,500,sometimes\n", "price_mode", "price_mode_invalid"),
        ("name,price,quantity\nA,500,-3\n", "quantity", "quantity_invalid"),
        ("name,price,quantity\nA,500,two\n", "quantity", "quantity_invalid"),
        ("name,price,category\n,500,Shoes\n", "name", "name_required"),
    ],
)
def test_row_errors(pg_client, shop, content, field, code):
    _, headers = shop
    rows = _post(pg_client, headers, PREVIEW, content).json()["data"]["rows"]
    assert rows[0]["status"] == "error"
    assert {"field": field, "code": code} in rows[0]["errors"]


@pytest.mark.parametrize("first", ["=", "+", "-", "@"])
def test_a_name_starting_with_a_formula_character_is_rejected(pg_client, shop, first):
    _, headers = shop
    content = f"name,price\n{first}SUM(A1),500\nOK {first}x,500\n"
    rows = _post(pg_client, headers, PREVIEW, content).json()["data"]["rows"]
    assert rows[0]["errors"] == [{"field": "name", "code": "name_formula"}]
    assert rows[1]["status"] == "create"  # only the first character matters


def test_duplicate_external_ref_in_the_file_is_flagged(pg_client, shop):
    _, headers = shop
    rows = _post(
        pg_client, headers, PREVIEW, "name,price,external_ref\nA,1,SKU-1\nB,2,SKU-1\n"
    ).json()["data"]["rows"]
    assert rows[0]["status"] == "create"
    assert rows[1]["errors"] == [{"field": "external_ref", "code": "duplicate_in_file"}]


# --- limits and unreadable ------------------------------------------------------------------


def test_500_rows_are_allowed_and_501_are_too_large(pg_client, db_session):
    tenant = make_tenant(db_session, "enterprise")
    _, headers = make_user(db_session, tenant)
    ok = "name,price\n" + "".join(f"Item {i},100\n" for i in range(500))
    assert _post(pg_client, headers, PREVIEW, ok).status_code == 200
    r = _post(pg_client, headers, PREVIEW, ok + "One more,100\n")
    assert r.status_code == 422 and r.json()["code"] == "import_too_large"
    assert "500" in r.json()["message"]


def test_over_256kb_is_too_large(pg_client, shop):
    _, headers = shop
    content = "name,price\n" + "x" * (256 * 1024) + ",100\n"
    r = _post(pg_client, headers, PREVIEW, content)
    assert r.status_code == 422 and r.json()["code"] == "import_too_large"


@pytest.mark.parametrize("content", ["", "price\n100\n", "name\n"])
def test_unreadable_content(pg_client, shop, content):
    _, headers = shop
    r = _post(pg_client, headers, PREVIEW, content)
    assert r.status_code == 422 and r.json()["code"] == "import_unreadable"


def test_unknown_format_or_mode_is_a_request_validation_error(pg_client, shop):
    _, headers = shop
    assert _post(pg_client, headers, PREVIEW, CSV, fmt="xlsx").status_code == 422
    assert _post(pg_client, headers, PREVIEW, CSV, mode="replace").status_code == 422


# --- apply ----------------------------------------------------------------------------------


def test_apply_creates_every_row_with_sequential_refs(pg_client, db_session, shop):
    tenant, headers = shop
    add_items(db_session, tenant, 2)
    r = _post(pg_client, headers, APPLY, CSV)
    assert r.status_code == 200, r.text
    assert r.json()["data"] == {"created": 2, "updated": 0, "skipped": 0, "errors": []}
    assert [(i.seq, i.name) for i in _items(db_session, tenant)] == [
        (1, "Item 1"),
        (2, "Item 2"),
        (3, "Canvas sneaker"),
        (4, "Haircut"),
    ]
    got = pg_client.get(f"{URL}?limit=10", headers=headers).json()["data"]
    assert got[0]["ref"] == "#4" and got[0]["kind"] == "service" and got[0]["price"] == 5000
    # the allocator moved on, so the next manual create is #5
    assert pg_client.post(URL, json=body(), headers=headers).json()["data"]["seq"] == 5


def test_apply_is_atomic_when_any_row_is_bad(pg_client, db_session, shop):
    tenant, headers = shop
    r = _post(pg_client, headers, APPLY, CSV + "Bad,product,abc,in stock,Shoes\n")
    assert r.status_code == 422
    err = r.json()
    assert err["code"] == "import_has_errors"
    assert err["errors"] == [{"row": 3, "errors": [{"field": "price", "code": "price_invalid"}]}]
    assert err["summary"] == {"create": 2, "update": 0, "error": 1, "over_limit_by": 0}
    assert _items(db_session, tenant) == []


def test_skip_errors_imports_the_valid_rows_and_reports_the_rest(pg_client, db_session, shop):
    tenant, headers = shop
    r = _post(pg_client, headers, APPLY, CSV + "Bad,product,abc,in stock,Shoes\n", skip_errors=True)
    assert r.status_code == 200, r.text
    assert r.json()["data"] == {
        "created": 2,
        "updated": 0,
        "skipped": 1,
        "errors": [{"row": 3, "errors": [{"field": "price", "code": "price_invalid"}]}],
    }
    assert len(_items(db_session, tenant)) == 2


def test_apply_records_an_activity_day(pg_client, db_session, shop):
    tenant, headers = shop
    _post(pg_client, headers, APPLY, CSV)
    days = db_session.query(OfferingActivityDay).filter_by(tenant_id=tenant.id).all()
    assert [d.day for d in days] == [datetime.now(UTC).date()]


# --- cap ------------------------------------------------------------------------------------


def _trial(db_session, active):
    tenant = make_tenant(db_session, "trial")  # cap 25
    _, headers = make_user(db_session, tenant)
    add_items(db_session, tenant, active)
    return tenant, headers


def _many(n, start=1):
    return "name,price\n" + "".join(f"New {i},100\n" for i in range(start, start + n))


def test_preview_reports_how_far_over_the_cap_the_import_goes(pg_client, db_session):
    _, headers = _trial(db_session, 20)
    r = _post(pg_client, headers, PREVIEW, _many(10))
    assert r.json()["data"]["summary"] == {
        "create": 10,
        "update": 0,
        "error": 0,
        "over_limit_by": 5,
    }


def test_apply_over_the_cap_is_refused_whole_and_says_how_many_fit(pg_client, db_session):
    tenant, headers = _trial(db_session, 20)
    r = _post(pg_client, headers, APPLY, _many(10))
    assert r.status_code == 409
    assert r.json() == {
        "code": "offering_limit",
        "message": "Your plan allows 25 items and you have 20. This import would add 10; only 5 fit.",
        "limit": 25,
        "used": 20,
        "can_add": 5,
    }
    assert len(_items(db_session, tenant)) == 20


def test_apply_exactly_to_the_cap_works_and_updates_do_not_count(pg_client, db_session):
    tenant, headers = _trial(db_session, 20)
    assert _post(pg_client, headers, APPLY, _many(5)).status_code == 200
    content = "name,price\nItem 1,555\nNew 99,100\n"
    r = _post(pg_client, headers, APPLY, content, mode="upsert")
    assert r.status_code == 409  # Item 1 updates for free, New 99 is the 26th


def test_updates_alone_never_trip_the_cap(pg_client, db_session):
    tenant, headers = _trial(db_session, 25)
    r = _post(pg_client, headers, APPLY, "name,price\nItem 1,777\n", mode="upsert")
    assert r.status_code == 200 and r.json()["data"]["updated"] == 1


# --- upsert ---------------------------------------------------------------------------------


def test_add_mode_never_touches_existing_items(pg_client, db_session, shop):
    tenant, headers = shop
    add_items(db_session, tenant, 1)
    r = _post(pg_client, headers, APPLY, "name,price\nItem 1,999\n", mode="add")
    assert r.json()["data"]["created"] == 1
    assert [i.price for i in _items(db_session, tenant)] == [1000, 999]


def test_add_mode_rejects_a_ref_that_already_exists(pg_client, db_session, shop):
    tenant, headers = shop
    add_items(db_session, tenant, 1, external_ref="SKU-1")
    rows = _post(
        pg_client, headers, PREVIEW, "name,price,external_ref\nA,5,SKU-1\n", mode="add"
    ).json()["data"]["rows"]
    assert rows[0]["errors"] == [{"field": "external_ref", "code": "external_ref_taken"}]


def test_upsert_updates_by_external_ref_and_creates_the_rest(pg_client, db_session, shop):
    tenant, headers = shop
    add_items(db_session, tenant, 1, external_ref="SKU-1", name="Old name")
    content = "name,price,external_ref\nNew name,2500,SKU-1\nFresh,100,SKU-2\n"
    prev = _post(pg_client, headers, PREVIEW, content, mode="upsert").json()["data"]
    assert [r["status"] for r in prev["rows"]] == ["update", "create"]
    assert prev["rows"][0]["target"] == "#1"
    r = _post(pg_client, headers, APPLY, content, mode="upsert")
    assert r.json()["data"] == {"created": 1, "updated": 1, "skipped": 0, "errors": []}
    items = _items(db_session, tenant)
    assert [(i.seq, i.name, i.price, i.external_ref) for i in items] == [
        (1, "New name", 2500, "SKU-1"),
        (2, "Fresh", 100, "SKU-2"),
    ]


def test_upsert_without_a_ref_matches_the_name_case_insensitively(pg_client, db_session, shop):
    tenant, headers = shop
    add_items(db_session, tenant, 1, name="Canvas Sneaker")
    r = _post(pg_client, headers, APPLY, "name,price\ncanvas sneaker,4200\n", mode="upsert")
    assert r.json()["data"]["updated"] == 1 and r.json()["data"]["created"] == 0
    assert [i.price for i in _items(db_session, tenant)] == [4200]


def test_two_active_items_with_the_same_name_make_the_row_ambiguous(pg_client, db_session, shop):
    tenant, headers = shop
    add_items(db_session, tenant, 2, name="Twin")
    rows = _post(pg_client, headers, PREVIEW, "name,price\nTwin,5\n", mode="upsert").json()["data"][
        "rows"
    ]
    assert rows[0]["errors"] == [{"field": "name", "code": "ambiguous_name"}]


def test_blank_cells_and_absent_columns_leave_an_existing_item_unchanged(
    pg_client, db_session, shop
):
    tenant, headers = shop
    add_items(
        db_session, tenant, 1, name="Keep", category="Shoes", description="Black", negotiable=True
    )
    _post(pg_client, headers, APPLY, "name,price,category\nKeep,2000,\n", mode="upsert")
    item = _items(db_session, tenant)[0]
    assert (item.price, item.category, item.description, item.negotiable) == (
        2000,
        "Shoes",
        "Black",
        True,
    )


def test_upsert_name_match_ignores_archived_items(pg_client, db_session, shop):
    tenant, headers = shop
    add_items(db_session, tenant, 1, name="Gone", status=OfferingStatus.ARCHIVED)
    r = _post(pg_client, headers, APPLY, "name,price\nGone,50\n", mode="upsert")
    assert r.json()["data"]["created"] == 1
    assert (
        len(_items(db_session, tenant)) == 1
        and len(_items(db_session, tenant, OfferingStatus.ARCHIVED)) == 1
    )


def test_upsert_by_ref_reactivates_an_archived_item_and_counts_toward_the_cap(
    pg_client, db_session
):
    tenant, headers = _trial(db_session, 25)
    add_items(db_session, tenant, 1, start=100, external_ref="OLD", status=OfferingStatus.ARCHIVED)
    r = _post(pg_client, headers, APPLY, "name,price,external_ref\nBack,5,OLD\n", mode="upsert")
    assert r.status_code == 409 and r.json()["code"] == "offering_limit"


def test_upsert_by_ref_reactivation_within_the_cap(pg_client, db_session, shop):
    tenant, headers = shop
    add_items(db_session, tenant, 1, external_ref="OLD", status=OfferingStatus.ARCHIVED)
    r = _post(pg_client, headers, APPLY, "name,price,external_ref\nBack,5,OLD\n", mode="upsert")
    assert r.status_code == 200 and r.json()["data"]["updated"] == 1
    item = _items(db_session, tenant)[0]
    assert item.name == "Back" and item.archived_at is None


def test_availability_changed_at_moves_on_an_availability_update(pg_client, db_session, shop):
    tenant, headers = shop
    add_items(
        db_session,
        tenant,
        1,
        name="Shoe",
        availability_changed_at=datetime.now(UTC) - timedelta(days=3),
    )
    before = _items(db_session, tenant)[0].availability_changed_at
    _post(pg_client, headers, APPLY, "name,availability\nShoe,sold out\n", mode="upsert")
    after = _items(db_session, tenant)[0]
    assert after.availability.value == "out_of_stock" and after.availability_changed_at > before


def test_another_tenants_ref_or_name_never_matches(pg_client, db_session, shop):
    tenant, headers = shop
    other = make_tenant(db_session, "lite")
    add_items(db_session, other, 1, name="Shared", external_ref="SKU-9")
    r = _post(pg_client, headers, APPLY, "name,price,external_ref\nShared,5,SKU-9\n", mode="upsert")
    assert r.json()["data"] == {"created": 1, "updated": 0, "skipped": 0, "errors": []}
    assert [i.price for i in _items(db_session, other)] == [1000]


# --- auth -----------------------------------------------------------------------------------


def test_unauthenticated_and_agent_are_refused(pg_client, db_session, shop):
    tenant, _ = shop
    _, agent = make_user(db_session, tenant, UserRole.SUPPORT_AGENT)
    for url in (PREVIEW, APPLY):
        assert (
            pg_client.post(url, json={"format": "csv", "content": CSV, "mode": "add"}).status_code
            == 401
        )
        assert _post(pg_client, agent, url, CSV).status_code == 403


def test_a_lapsed_tenant_cannot_apply(pg_client, db_session, shop):
    tenant, headers = shop
    tenant.subscription_status = "active"
    tenant.subscription_ends_at = datetime.now(UTC) - timedelta(days=1)
    db_session.commit()
    r = _post(pg_client, headers, APPLY, CSV)
    assert r.status_code == 403 and r.json()["code"] == "feature_not_in_plan"
    assert _items(db_session, tenant) == []
