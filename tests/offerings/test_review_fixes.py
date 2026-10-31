"""Regressions from the independent review of the S1 core (stall, 500s, normalisation bypass).

Each test names the defect it pins. The invisible characters are written as escapes on purpose.
"""

import time

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models.offering import Offering
from app.services import offering_import
from tests.entitlements.helpers import make_tenant, make_user
from tests.offerings.helpers import URL, add_items, body

PREVIEW = f"{URL}/import/preview"
APPLY = f"{URL}/import"
ZWSP = "​"
BOM = "﻿"


@pytest.fixture
def shop(db_session):
    tenant = make_tenant(db_session, "professional")
    _, headers = make_user(db_session, tenant)
    return tenant, headers


def _post(client, headers, url, content, fmt="csv", mode="add", **kw):
    return client.post(
        url, json={"format": fmt, "content": content, "mode": mode, **kw}, headers=headers
    )


def _rows(client, headers, content, **kw):
    r = _post(client, headers, PREVIEW, content, **kw)
    assert r.status_code == 200, r.text
    return r.json()["data"]["rows"]


# --- 1. the lines parser must stay linear -----------------------------------------------------


def test_a_huge_whitespace_line_is_refused_fast_not_after_minutes(pg_client, shop):
    _, headers = shop
    content = "a" + " " * 20_000 + "a"  # was ~3 s at 20k, ~9 min at the 256 KB cap
    started = time.perf_counter()
    r = _post(pg_client, headers, PREVIEW, content, fmt="lines")
    elapsed = time.perf_counter() - started
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "line_too_long"
    assert "line 1" in r.json()["message"].lower()
    assert elapsed < 0.5


@pytest.mark.parametrize("filler", [" ", "\t", " ", " |", " - "])
def test_parse_lines_is_linear_on_every_separator_shape(filler):
    line = "a" + filler * (900 // len(filler)) + "a"  # inside the cap, worst shape for the regex
    started = time.perf_counter()
    for _ in range(200):
        offering_import.parse_lines(line)
    assert time.perf_counter() - started < 1.0


def test_whitespace_runs_inside_a_line_collapse_to_one_space():
    rows = offering_import.parse_lines("Canvas     sneaker  -   12000   -  in stock")
    assert rows == [(1, {"name": "Canvas sneaker", "price": "12000", "availability": "in stock"})]


def test_a_line_of_exactly_the_cap_is_allowed():
    assert offering_import.parse_lines("x" * offering_import.MAX_LINE_CHARS)


@pytest.mark.parametrize(
    "cell",
    [" " * 120_000 + "x", "n" + " " * 120_000 + "x", "1" * 120_000 + "x", "1," * 60_000 + "x"],
)
def test_csv_cells_cannot_stall_the_amount_parser(cell):
    started = time.perf_counter()
    rows = [(1, {"name": "A", "price": cell, "quantity": cell})]
    plan = offering_import.normalize_row(rows[0][1])
    assert time.perf_counter() - started < 0.5
    assert plan[1]  # an error, not a hang


# --- 2. a null on a non-nullable field is a 422, not a 500 ------------------------------------


@pytest.mark.parametrize("field", ["kind", "price_mode", "availability"])
def test_patching_a_required_enum_to_null_is_a_validation_error(pg_client, shop, field):
    _, headers = shop
    made = pg_client.post(URL, json=body(), headers=headers).json()["data"]
    r = pg_client.patch(f"{URL}/{made['id']}", json={field: None}, headers=headers)
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "validation"
    assert {"field": field, "code": f"{field}_required"} in r.json()["errors"]
    after = pg_client.get(f"{URL}/{made['id']}", headers=headers).json()["data"]
    assert after[field] == made[field]


# --- 3. numeric bounds ------------------------------------------------------------------------


@pytest.mark.parametrize("quantity", [3_000_000_000, 2_147_483_648, -1])
def test_create_and_patch_refuse_an_out_of_range_quantity(pg_client, shop, quantity):
    _, headers = shop
    r = pg_client.post(URL, json=body(quantity=quantity), headers=headers)
    assert r.status_code == 422, r.text
    assert {"field": "quantity", "code": "quantity_invalid"} in r.json()["errors"]
    made = pg_client.post(URL, json=body(), headers=headers).json()["data"]
    r = pg_client.patch(f"{URL}/{made['id']}", json={"quantity": quantity}, headers=headers)
    assert r.status_code == 422, r.text
    assert {"field": "quantity", "code": "quantity_invalid"} in r.json()["errors"]


def test_the_largest_quantity_the_column_holds_is_accepted(pg_client, shop):
    _, headers = shop
    r = pg_client.post(URL, json=body(quantity=2_147_483_647), headers=headers)
    assert r.status_code == 201 and r.json()["data"]["quantity"] == 2_147_483_647


def test_an_out_of_range_csv_quantity_is_a_row_error_not_a_500(pg_client, shop):
    _, headers = shop
    content = "name,price,quantity\nA,100,3000000000\n"
    assert _rows(pg_client, headers, content)[0]["errors"] == [
        {"field": "quantity", "code": "quantity_invalid"}
    ]
    r = _post(pg_client, headers, APPLY, content)
    assert r.status_code == 422 and r.json()["code"] == "import_has_errors"


@pytest.mark.parametrize("price", [10_000_000_001, 0, -5])
def test_price_bounds_are_enforced_on_create(pg_client, shop, price):
    _, headers = shop
    r = pg_client.post(URL, json=body(price=price), headers=headers)
    assert r.status_code == 422
    assert {"field": "price", "code": "price_out_of_range"} in r.json()["errors"]


# --- 4. import and CRUD normalise text the same way before they compare it --------------------


def test_refs_that_differ_only_by_invisible_characters_are_duplicates(pg_client, shop):
    _, headers = shop
    content = f"name,sku\nX,A\nY,A{ZWSP}\n"
    rows = _rows(pg_client, headers, content)
    assert rows[0]["status"] == "create"
    assert rows[1]["errors"] == [{"field": "external_ref", "code": "duplicate_in_file"}]
    r = _post(pg_client, headers, APPLY, content)
    assert r.status_code == 422 and r.json()["code"] == "import_has_errors"  # was a 500


def test_an_incoming_ref_with_a_bom_meets_the_stored_ref_in_add_mode(pg_client, db_session, shop):
    tenant, headers = shop
    add_items(db_session, tenant, 1, external_ref="A")
    rows = _rows(pg_client, headers, f"name,sku\nNew,{BOM}A\n")
    assert rows[0]["errors"] == [{"field": "external_ref", "code": "external_ref_taken"}]
    r = _post(pg_client, headers, APPLY, f"name,sku\nNew,{BOM}A\n")
    assert r.status_code == 422 and r.json()["code"] == "import_has_errors"


def test_an_incoming_ref_with_a_zero_width_space_updates_the_stored_item_in_upsert(
    pg_client, db_session, shop
):
    tenant, headers = shop
    add_items(db_session, tenant, 1, external_ref="A")
    rows = _rows(pg_client, headers, f"name,sku,price\nRenamed,A{ZWSP},700\n", mode="upsert")
    assert rows[0]["status"] == "update" and rows[0]["target"] == "#1"
    assert _post(
        pg_client, headers, APPLY, f"name,sku,price\nRenamed,A{ZWSP},700\n", mode="upsert"
    ).json()["data"] == {"created": 0, "updated": 1, "skipped": 0, "errors": []}
    db_session.expire_all()
    assert db_session.query(Offering).filter_by(tenant_id=tenant.id).count() == 1


@pytest.mark.parametrize("name", [f"{ZWSP}=HYPERLINK(1)", "\x01+cmd", f"{BOM}@SUM(A1)", "‮-1"])
def test_the_formula_guard_runs_after_text_cleaning(pg_client, shop, name):
    _, headers = shop
    rows = _rows(pg_client, headers, f"name,price\n{name},500\n")
    assert rows[0]["errors"] == [{"field": "name", "code": "name_formula"}]


def test_a_name_that_is_blank_after_cleaning_is_name_required(pg_client, shop):
    _, headers = shop
    rows = _rows(pg_client, headers, f"name,price\n{ZWSP},500\n")
    assert {"field": "name", "code": "name_required"} in rows[0]["errors"]


@pytest.mark.parametrize("name", ["=1+1", f"{ZWSP}+cmd", "@x"])
def test_crud_create_and_patch_apply_the_same_formula_guard(pg_client, shop, name):
    _, headers = shop
    r = pg_client.post(URL, json=body(name=name), headers=headers)
    assert r.status_code == 422, r.text
    assert {"field": "name", "code": "name_formula"} in r.json()["errors"]
    made = pg_client.post(URL, json=body(), headers=headers).json()["data"]
    r = pg_client.patch(f"{URL}/{made['id']}", json={"name": name}, headers=headers)
    assert r.status_code == 422 and {"field": "name", "code": "name_formula"} in r.json()["errors"]


def test_an_integrity_error_on_apply_is_a_clean_409_not_a_500(pg_client, shop, monkeypatch):
    _, headers = shop

    real_flush = Session.flush

    def boom(self, *args, **kwargs):
        if any(isinstance(obj, Offering) for obj in self.new):
            raise IntegrityError("insert", {}, Exception("duplicate key"))
        return real_flush(self, *args, **kwargs)

    monkeypatch.setattr(Session, "flush", boom)
    r = _post(pg_client, headers, APPLY, "name,price\nA,100\n")
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "import_conflict"
    assert "duplicate key" not in r.text


def test_an_absurdly_large_import_body_is_refused_by_the_schema(pg_client, shop):
    _, headers = shop
    r = _post(pg_client, headers, PREVIEW, "x" * 1_048_577, fmt="lines")
    assert r.status_code == 422
    assert "detail" in r.json()  # the framework shape, documented in the FE guide


# --- plausible findings: bounded storage, limiter key, event loop, sweep cost -----------------

from unittest.mock import patch  # noqa: E402

from app.db.models.offering import OfferingStatus  # noqa: E402
from app.workers.offering_cleanup import MAX_SWEEP_PAGES, sweep_orphan_offering_images  # noqa: E402
from tests.offerings.conftest import make_image  # noqa: E402,F401  (fixtures: cloud, moderation)


def _upload(client, headers, item_id):
    return client.post(
        f"{URL}/{item_id}/images",
        files={"file": ("a.jpg", make_image(), "image/jpeg")},
        headers=headers,
    )


@pytest.fixture
def lite_shop(db_session, cloud, moderation):
    tenant = make_tenant(db_session, "lite")  # 100 items, 4 photos each
    _, headers = make_user(db_session, tenant)
    return tenant, headers


def test_an_archived_item_takes_no_new_photos(pg_client, db_session, lite_shop, cloud, moderation):
    tenant, headers = lite_shop
    made = pg_client.post(URL, json=body(), headers=headers).json()["data"]
    assert pg_client.delete(f"{URL}/{made['id']}", headers=headers).status_code == 204
    r = _upload(pg_client, headers, made["id"])
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "offering_archived"
    assert cloud.uploads == [] and moderation.await_count == 0


def test_total_rows_including_archived_have_a_ceiling_per_plan(pg_client, db_session, lite_shop):
    tenant, headers = lite_shop
    add_items(db_session, tenant, 400, status=OfferingStatus.ARCHIVED)  # 4 x the active cap
    r = pg_client.post(URL, json=body(), headers=headers)
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "offering_archive_full"
    assert r.json()["limit"] == 400 and r.json()["used"] == 400


def test_the_ceiling_counts_an_import_and_names_how_many_fit(pg_client, db_session, lite_shop):
    tenant, headers = lite_shop
    add_items(db_session, tenant, 399, status=OfferingStatus.ARCHIVED)
    r = _post(pg_client, headers, APPLY, "name,price\nA,100\nB,100\n")
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "offering_archive_full" and r.json()["can_add"] == 1


def test_deleting_an_archived_item_for_good_frees_a_slot(pg_client, db_session, lite_shop):
    tenant, headers = lite_shop
    add_items(db_session, tenant, 400, status=OfferingStatus.ARCHIVED)
    victim = db_session.query(Offering).filter_by(tenant_id=tenant.id, seq=1).one()
    assert pg_client.delete(f"{URL}/{victim.id}?hard=true", headers=headers).status_code == 204
    assert pg_client.post(URL, json=body(), headers=headers).status_code == 201


def test_unarchiving_does_not_need_a_free_row_slot(pg_client, db_session, lite_shop):
    tenant, headers = lite_shop
    add_items(db_session, tenant, 400, status=OfferingStatus.ARCHIVED)
    item = db_session.query(Offering).filter_by(tenant_id=tenant.id, seq=7).one()
    r = pg_client.patch(f"{URL}/{item.id}", json={"status": "active"}, headers=headers)
    assert r.status_code == 200, r.text


def test_the_upload_budget_is_also_kept_per_tenant(pg_client, db_session, lite_shop, monkeypatch):
    tenant, headers = lite_shop
    made = pg_client.post(URL, json=body(), headers=headers).json()["data"]
    seen = []

    async def record(limiter, identifier):
        seen.append((limiter.key_prefix, identifier))

    monkeypatch.setattr("app.api.v1.endpoints.offerings.enforce_identifier_limit", record)
    assert _upload(pg_client, headers, made["id"]).status_code == 201
    assert seen == [("http_offering_upload_tenant", f"tenant:{tenant.id}")]


def test_blocking_work_runs_off_the_event_loop_and_no_transaction_spans_moderation(
    pg_client, db_session, lite_shop, cloud, monkeypatch
):
    import threading

    from app.services.offering_service import OfferingService

    tenant, headers = lite_shop
    made = pg_client.post(URL, json=body(), headers=headers).json()["data"]
    events: list[tuple[str, int]] = []
    real_gate = OfferingService.require_write
    real_commit = db_session.commit

    def gate(self, tenant_id):
        events.append(("gate", threading.get_ident()))
        return real_gate(self, tenant_id)

    def commit():
        events.append(("commit", threading.get_ident()))
        return real_commit()

    async def moderate(content, content_type):
        events.append(("moderate", threading.get_ident()))
        return True, None

    monkeypatch.setattr(OfferingService, "require_write", gate)
    monkeypatch.setattr(db_session, "commit", commit)
    monkeypatch.setattr("app.services.offering_images.moderate_image", moderate)
    events.clear()
    assert _upload(pg_client, headers, made["id"]).status_code == 201
    names = [name for name, _ in events]
    loop_thread = dict(events)["moderate"]
    assert dict(events)["gate"] != loop_thread  # the database gate is not run on the loop
    assert names.index("commit") < names.index("moderate")  # connection released before the wait


async def test_the_sweep_stops_after_a_page_budget_so_the_admin_api_quota_is_safe(
    db_session, monkeypatch
):
    monkeypatch.setattr("app.workers.offering_cleanup.SessionLocal", lambda: db_session)
    monkeypatch.setattr(db_session, "close", lambda: None)
    endless = patch(
        "app.workers.offering_cleanup.cloudinary_manager.list_tagged_images",
        side_effect=[([], "more")] * 200 + [([], None)],  # a long listing that does end
    )
    with endless as listing:
        await sweep_orphan_offering_images({})
    assert listing.call_count == MAX_SWEEP_PAGES


def test_an_item_deleted_while_its_photo_uploads_is_404_and_the_asset_is_destroyed(
    pg_client, db_session, lite_shop, cloud, moderation
):
    tenant, headers = lite_shop
    made = pg_client.post(URL, json=body(), headers=headers).json()["data"]

    async def delete_meanwhile(content, content_type):
        db_session.query(Offering).filter_by(id=made["id"]).delete()
        db_session.commit()
        return True, None

    moderation.side_effect = delete_meanwhile
    r = _upload(pg_client, headers, made["id"])
    assert r.status_code == 404 and r.json()["code"] == "offering_not_found"
    assert len(cloud.uploads) == 1 and len(cloud.destroyed) == 1
