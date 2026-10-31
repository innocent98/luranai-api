"""Offerings CRUD: create/list/get/patch/archive/delete, caps, validation, auth, lapsed gate."""

from datetime import UTC, date, datetime, timedelta

import pytest

from app.db.models.offering import (
    Offering,
    OfferingActivityDay,
    OfferingStatus,
)
from app.db.models.user import UserRole
from tests.entitlements.helpers import make_tenant, make_user
from tests.offerings.helpers import URL, add_items, body


@pytest.fixture
def admin(db_session):
    tenant = make_tenant(db_session, "professional")
    _, headers = make_user(db_session, tenant)
    return tenant, headers


def _post(client, headers, **kw):
    return client.post(URL, json=body(**kw), headers=headers)


# --- create ---------------------------------------------------------------------------------


def test_create_returns_the_item_with_a_seq_and_defaults(pg_client, admin):
    _, headers = admin
    r = _post(pg_client, headers, description="Black, size 42", category="Shoes")
    assert r.status_code == 201, r.text
    data = r.json()["data"]
    assert data["seq"] == 1 and data["ref"] == "#1"
    assert data["kind"] == "product" and data["name"] == "Canvas sneaker"
    assert data["price_mode"] == "fixed" and data["price"] == 12000
    assert data["status"] == "active" and data["negotiable"] is False
    assert data["images"] == [] and data["photo_count"] == 0 and data["cover_url"] is None
    assert data["description"] == "Black, size 42" and data["category"] == "Shoes"
    assert _post(pg_client, headers, name="Second").json()["data"]["ref"] == "#2"


def test_seq_is_per_tenant_and_never_reused(pg_client, db_session, admin):
    tenant, headers = admin
    first = _post(pg_client, headers).json()["data"]
    assert pg_client.delete(f"{URL}/{first['id']}?hard=true", headers=headers).status_code == 204
    assert _post(pg_client, headers).json()["data"]["seq"] == 2  # #1 is not handed out again
    other = make_tenant(db_session, "lite")
    _, other_headers = make_user(db_session, other)
    assert _post(pg_client, other_headers).json()["data"]["seq"] == 1


@pytest.mark.parametrize(
    "kw,code,field",
    [
        ({"price": None}, "price_required", "price"),
        ({"price_mode": "from", "price": None}, "price_required", "price"),
        ({"price_mode": "on_request", "price": 500}, "price_not_allowed", "price"),
        ({"price": 0}, "price_out_of_range", "price"),
        ({"price": -5}, "price_out_of_range", "price"),
        ({"price": 10_000_000_001}, "price_out_of_range", "price"),
        ({"name": "   "}, "name_required", "name"),
        ({"name": "x" * 121}, "name_too_long", "name"),
        ({"description": "x" * 2001}, "description_too_long", "description"),
        ({"quantity": -1}, "quantity_invalid", "quantity"),
    ],
)
def test_validation_codes(pg_client, admin, kw, code, field):
    _, headers = admin
    r = _post(pg_client, headers, **kw)
    assert r.status_code == 422, r.text
    err = r.json()
    assert err["code"] == "validation"
    assert {"field": field, "code": code} in [
        {"field": e["field"], "code": e["code"]} for e in err["errors"]
    ]


def test_on_request_item_takes_no_price(pg_client, admin):
    _, headers = admin
    r = _post(pg_client, headers, price_mode="on_request", price=None, kind="service")
    assert r.status_code == 201 and r.json()["data"]["price"] is None


def test_all_validation_errors_come_back_together(pg_client, admin):
    _, headers = admin
    r = _post(pg_client, headers, name="", price=None)
    codes = {e["code"] for e in r.json()["errors"]}
    assert codes == {"name_required", "price_required"}


def test_control_characters_are_stripped_from_text_fields(pg_client, admin):
    _, headers = admin
    r = _post(pg_client, headers, name="Sneaker\x00\x07", description="line1\nline2\x1b[0m")
    data = r.json()["data"]
    assert data["name"] == "Sneaker"
    assert data["description"] == "line1\nline2[0m"


def test_upper_price_bound_is_accepted(pg_client, admin):
    _, headers = admin
    assert _post(pg_client, headers, price=10_000_000_000).status_code == 201


# --- caps -----------------------------------------------------------------------------------


def test_trial_cap_is_enforced_with_limit_and_used(pg_client, db_session):
    tenant = make_tenant(db_session, "trial")
    _, headers = make_user(db_session, tenant)
    add_items(db_session, tenant, 25)
    r = _post(pg_client, headers)
    assert r.status_code == 409
    assert r.json() == {
        "code": "offering_limit",
        "message": "Your plan allows 25 items. Archive or delete one, or upgrade, to add more.",
        "limit": 25,
        "used": 25,
    }


def test_archived_items_do_not_count_toward_the_cap(pg_client, db_session):
    tenant = make_tenant(db_session, "trial")
    _, headers = make_user(db_session, tenant)
    add_items(db_session, tenant, 24)
    add_items(db_session, tenant, 10, status=OfferingStatus.ARCHIVED, start=100)
    assert _post(pg_client, headers).status_code == 201
    assert _post(pg_client, headers, name="26th").status_code == 409


def test_enterprise_has_no_cap(pg_client, db_session):
    tenant = make_tenant(db_session, "enterprise")
    _, headers = make_user(db_session, tenant)
    add_items(db_session, tenant, 3000)
    assert _post(pg_client, headers).status_code == 201


def test_unarchiving_over_the_cap_is_refused(pg_client, db_session):
    tenant = make_tenant(db_session, "trial")
    _, headers = make_user(db_session, tenant)
    add_items(db_session, tenant, 25)
    add_items(db_session, tenant, 1, status=OfferingStatus.ARCHIVED, start=200)
    archived = db_session.query(Offering).filter_by(tenant_id=tenant.id, seq=200).one()
    r = pg_client.patch(f"{URL}/{archived.id}", json={"status": "active"}, headers=headers)
    assert r.status_code == 409 and r.json()["code"] == "offering_limit"


def test_a_tenant_over_its_cap_can_still_edit_existing_items(pg_client, db_session):
    tenant = make_tenant(db_session, "trial")
    _, headers = make_user(db_session, tenant)
    add_items(db_session, tenant, 30)  # e.g. an override that was removed
    item = db_session.query(Offering).filter_by(tenant_id=tenant.id, seq=1).one()
    r = pg_client.patch(f"{URL}/{item.id}", json={"name": "Renamed"}, headers=headers)
    assert r.status_code == 200 and r.json()["data"]["name"] == "Renamed"


# --- auth and lapsed ------------------------------------------------------------------------


def test_unauthenticated_is_401(pg_client):
    assert pg_client.get(URL).status_code == 401


def test_support_agent_can_read_but_not_write(pg_client, db_session):
    tenant = make_tenant(db_session, "lite")
    _, agent_headers = make_user(db_session, tenant, UserRole.SUPPORT_AGENT)
    _, admin_headers = make_user(db_session, tenant)
    item = _post(pg_client, admin_headers).json()["data"]
    assert pg_client.get(URL, headers=agent_headers).status_code == 200
    assert pg_client.get(f"{URL}/{item['id']}", headers=agent_headers).status_code == 200
    assert _post(pg_client, agent_headers).status_code == 403
    assert (
        pg_client.patch(
            f"{URL}/{item['id']}", json={"name": "x"}, headers=agent_headers
        ).status_code
        == 403
    )
    assert pg_client.delete(f"{URL}/{item['id']}", headers=agent_headers).status_code == 403


def test_lapsed_tenant_reads_but_cannot_write(pg_client, db_session):
    tenant = make_tenant(db_session, "lite")
    _, headers = make_user(db_session, tenant)
    item = _post(pg_client, headers).json()["data"]
    tenant.subscription_status = "active"
    tenant.subscription_ends_at = datetime.now(UTC) - timedelta(days=2)
    db_session.commit()
    assert pg_client.get(URL, headers=headers).status_code == 200
    assert pg_client.get(f"{URL}/{item['id']}", headers=headers).status_code == 200
    for r in (
        _post(pg_client, headers),
        pg_client.patch(f"{URL}/{item['id']}", json={"name": "x"}, headers=headers),
        pg_client.delete(f"{URL}/{item['id']}", headers=headers),
    ):
        assert r.status_code == 403, r.text
        assert r.json()["code"] == "feature_not_in_plan" and r.json()["feature"] == "offerings"


def test_another_tenants_item_is_a_404(pg_client, db_session, admin):
    _, headers = admin
    other = make_tenant(db_session, "lite")
    _, other_headers = make_user(db_session, other)
    item = _post(pg_client, other_headers).json()["data"]
    for r in (
        pg_client.get(f"{URL}/{item['id']}", headers=headers),
        pg_client.patch(f"{URL}/{item['id']}", json={"name": "x"}, headers=headers),
        pg_client.delete(f"{URL}/{item['id']}", headers=headers),
        pg_client.delete(f"{URL}/{item['id']}?hard=true", headers=headers),
    ):
        assert r.status_code == 404
        assert r.json() == {"code": "offering_not_found", "message": "Offering not found"}
    assert pg_client.get(URL, headers=headers).json()["data"] == []


# --- get / list -----------------------------------------------------------------------------


def test_get_by_id(pg_client, admin):
    _, headers = admin
    item = _post(pg_client, headers).json()["data"]
    r = pg_client.get(f"{URL}/{item['id']}", headers=headers)
    assert r.status_code == 200 and r.json() == {"data": item}


def test_list_defaults_to_active_newest_first_with_meter_meta(pg_client, db_session):
    tenant = make_tenant(db_session, "lite")
    _, headers = make_user(db_session, tenant)
    for n in ("A", "B", "C"):
        _post(pg_client, headers, name=n)
    r = pg_client.get(URL, headers=headers)
    assert [i["name"] for i in r.json()["data"]] == ["C", "B", "A"]
    assert r.json()["meta"] == {
        "next_cursor": None,
        "total": 3,
        "page_size": 50,
        "limit": 100,
        "used": 3,
    }


def test_list_filters(pg_client, db_session):
    tenant = make_tenant(db_session, "lite")
    _, headers = make_user(db_session, tenant)
    _post(pg_client, headers, name="Red heels", category="Shoes", keywords="stilettos")
    _post(pg_client, headers, name="Tote bag", category="Bags")
    _post(
        pg_client,
        headers,
        name="Haircut",
        kind="service",
        category="Services",
        availability="out_of_stock",
    )

    def names(**params):
        r = pg_client.get(URL, params=params, headers=headers)
        assert r.status_code == 200, r.text
        return sorted(i["name"] for i in r.json()["data"])

    assert names(kind="service") == ["Haircut"]
    assert names(category="Bags") == ["Tote bag"]
    assert names(availability="out_of_stock") == ["Haircut"]
    assert names(q="stiletto") == ["Red heels"]  # keywords are searched
    assert names(q="TOTE") == ["Tote bag"]  # case-insensitive
    assert names(q="shoes") == ["Red heels"]  # category is searched


def test_list_search_treats_like_wildcards_as_text(pg_client, db_session):
    tenant = make_tenant(db_session, "lite")
    _, headers = make_user(db_session, tenant)
    _post(pg_client, headers, name="100% cotton")
    _post(pg_client, headers, name="Plain")
    r = pg_client.get(URL, params={"q": "%"}, headers=headers)
    assert [i["name"] for i in r.json()["data"]] == ["100% cotton"]


def test_archived_list_and_status_filter(pg_client, db_session):
    tenant = make_tenant(db_session, "lite")
    _, headers = make_user(db_session, tenant)
    a = _post(pg_client, headers, name="Keep").json()["data"]
    b = _post(pg_client, headers, name="Retire").json()["data"]
    pg_client.delete(f"{URL}/{b['id']}", headers=headers)
    assert [i["name"] for i in pg_client.get(URL, headers=headers).json()["data"]] == ["Keep"]
    r = pg_client.get(URL, params={"status": "archived"}, headers=headers)
    assert [i["name"] for i in r.json()["data"]] == ["Retire"]
    assert r.json()["data"][0]["archived_at"] is not None
    assert a["id"] != b["id"]


def test_cursor_pagination_walks_every_item_once(pg_client, db_session):
    tenant = make_tenant(db_session, "professional")
    _, headers = make_user(db_session, tenant)
    add_items(db_session, tenant, 7)
    seen, cursor = [], None
    for _ in range(5):
        params = {"limit": 3, **({"cursor": cursor} if cursor else {})}
        r = pg_client.get(URL, params=params, headers=headers)
        assert r.status_code == 200
        seen += [i["seq"] for i in r.json()["data"]]
        cursor = r.json()["meta"]["next_cursor"]
        assert r.json()["meta"]["total"] == 7
        if cursor is None:
            break
    assert seen == [7, 6, 5, 4, 3, 2, 1]


def test_list_limit_is_capped_at_100_and_bad_cursor_is_rejected(pg_client, admin):
    _, headers = admin
    assert pg_client.get(URL, params={"limit": 101}, headers=headers).status_code == 422
    r = pg_client.get(URL, params={"cursor": "nope"}, headers=headers)
    assert r.status_code == 422 and r.json()["code"] == "invalid_cursor"


# --- patch / archive / delete ---------------------------------------------------------------


def test_patch_changes_only_the_given_fields(pg_client, admin):
    _, headers = admin
    item = _post(pg_client, headers, category="Shoes", description="d").json()["data"]
    r = pg_client.patch(f"{URL}/{item['id']}", json={"price": 15000}, headers=headers)
    data = r.json()["data"]
    assert data["price"] == 15000 and data["category"] == "Shoes" and data["description"] == "d"
    assert data["seq"] == item["seq"]


def test_patch_can_clear_a_nullable_field_with_an_explicit_null(pg_client, admin):
    _, headers = admin
    item = _post(pg_client, headers, category="Shoes").json()["data"]
    r = pg_client.patch(f"{URL}/{item['id']}", json={"category": None}, headers=headers)
    assert r.json()["data"]["category"] is None


def test_availability_changed_at_moves_only_when_availability_changes(pg_client, admin):
    _, headers = admin
    item = _post(pg_client, headers).json()["data"]
    same = pg_client.patch(f"{URL}/{item['id']}", json={"name": "New"}, headers=headers)
    assert same.json()["data"]["availability_changed_at"] == item["availability_changed_at"]
    changed = pg_client.patch(
        f"{URL}/{item['id']}", json={"availability": "out_of_stock"}, headers=headers
    )
    assert changed.json()["data"]["availability"] == "out_of_stock"
    assert changed.json()["data"]["availability_changed_at"] > item["availability_changed_at"]


def test_switching_to_on_request_clears_the_price_and_back_needs_one(pg_client, admin):
    _, headers = admin
    item = _post(pg_client, headers).json()["data"]
    r = pg_client.patch(f"{URL}/{item['id']}", json={"price_mode": "on_request"}, headers=headers)
    assert r.status_code == 200 and r.json()["data"]["price"] is None
    r = pg_client.patch(f"{URL}/{item['id']}", json={"price_mode": "fixed"}, headers=headers)
    assert r.status_code == 422 and r.json()["errors"][0]["code"] == "price_required"
    r = pg_client.patch(
        f"{URL}/{item['id']}", json={"price_mode": "fixed", "price": 900}, headers=headers
    )
    assert r.status_code == 200 and r.json()["data"]["price"] == 900


def test_patch_rejects_an_invalid_merged_state(pg_client, admin):
    _, headers = admin
    item = _post(pg_client, headers).json()["data"]
    r = pg_client.patch(f"{URL}/{item['id']}", json={"price": 0}, headers=headers)
    assert r.status_code == 422 and r.json()["errors"][0]["code"] == "price_out_of_range"
    r = pg_client.patch(f"{URL}/{item['id']}", json={"name": ""}, headers=headers)
    assert r.status_code == 422 and r.json()["errors"][0]["code"] == "name_required"


def test_archive_by_patch_and_by_delete_then_unarchive(pg_client, admin):
    _, headers = admin
    item = _post(pg_client, headers).json()["data"]
    r = pg_client.patch(f"{URL}/{item['id']}", json={"status": "archived"}, headers=headers)
    assert r.json()["data"]["status"] == "archived" and r.json()["data"]["archived_at"]
    r = pg_client.patch(f"{URL}/{item['id']}", json={"status": "active"}, headers=headers)
    assert r.json()["data"]["status"] == "active" and r.json()["data"]["archived_at"] is None
    assert pg_client.delete(f"{URL}/{item['id']}", headers=headers).status_code == 204
    got = pg_client.get(f"{URL}/{item['id']}", headers=headers).json()["data"]
    assert got["status"] == "archived"


def test_hard_delete_removes_the_row(pg_client, db_session, admin):
    tenant, headers = admin
    item = _post(pg_client, headers).json()["data"]
    r = pg_client.delete(f"{URL}/{item['id']}?hard=true", headers=headers)
    assert r.status_code == 204 and r.content == b""
    assert pg_client.get(f"{URL}/{item['id']}", headers=headers).status_code == 404
    assert db_session.query(Offering).filter_by(tenant_id=tenant.id).count() == 0


def test_unknown_id_is_404_and_bad_uuid_is_422(pg_client, admin):
    _, headers = admin
    r = pg_client.get(f"{URL}/00000000-0000-0000-0000-000000000000", headers=headers)
    assert r.status_code == 404 and r.json()["code"] == "offering_not_found"
    assert pg_client.get(f"{URL}/not-a-uuid", headers=headers).status_code == 422


def test_writes_record_an_activity_day(pg_client, db_session, admin):
    tenant, headers = admin
    item = _post(pg_client, headers).json()["data"]
    pg_client.patch(f"{URL}/{item['id']}", json={"availability": "limited"}, headers=headers)
    rows = db_session.query(OfferingActivityDay).filter_by(tenant_id=tenant.id).all()
    assert [r.day for r in rows] == [datetime.now(UTC).date()]
    assert isinstance(rows[0].day, date)
