"""Offering photos: upload (validate, moderate, optimise), cap, order, delete, hard delete."""

import pytest

from app.db.models.offering import Offering, OfferingImage
from app.db.models.user import UserRole
from tests.entitlements.helpers import make_tenant, make_user
from tests.offerings.conftest import make_image
from tests.offerings.helpers import URL, add_items, add_photos, body

BLOCKED_TEXT = "This image can't be accepted. Please try a different one."


@pytest.fixture
def shop(db_session, cloud, moderation):
    tenant = make_tenant(db_session, "lite")  # 4 photos per item
    _, headers = make_user(db_session, tenant)
    add_items(db_session, tenant, 1)
    item = db_session.query(Offering).filter_by(tenant_id=tenant.id).one()
    return tenant, headers, item


def _upload(client, headers, item, content=None, name="a.jpg", ctype="image/jpeg"):
    return client.post(
        f"{URL}/{item.id}/images",
        files={"file": (name, content if content is not None else make_image(), ctype)},
        headers=headers,
    )


def _photos(db, item):
    db.expire_all()
    return (
        db.query(OfferingImage)
        .filter_by(offering_id=item.id)
        .order_by(OfferingImage.position)
        .all()
    )


# --- upload ---------------------------------------------------------------------------------


def test_upload_returns_the_photo_and_appends_at_the_next_position(
    pg_client, db_session, shop, cloud
):
    tenant, headers, item = shop
    first = _upload(pg_client, headers, item)
    assert first.status_code == 201, first.text
    data = first.json()["data"]
    assert data["position"] == 0 and data["width"] == 1200 and data["bytes"] == 240_000
    assert data["url"].startswith("https://res.cloudinary.com/demo/image/upload/v1700000000/")
    assert "/upload/c_limit,w_400,q_auto,f_jpg/" in data["thumb_url"]
    assert "/upload/c_limit,w_1000,q_auto,f_jpg/" in data["gallery_url"]
    assert _upload(pg_client, headers, item).json()["data"]["position"] == 1
    rows = _photos(db_session, item)
    assert [r.position for r in rows] == [0, 1] and rows[0].tenant_id == tenant.id
    assert rows[0].cloudinary_public_id.startswith(f"luran/offerings/{tenant.id}/")


def test_the_item_shows_its_photos_and_a_cover_thumbnail(pg_client, shop):
    _, headers, item = shop
    _upload(pg_client, headers, item)
    got = pg_client.get(f"{URL}/{item.id}", headers=headers).json()["data"]
    assert got["photo_count"] == 1 and len(got["images"]) == 1
    assert got["cover_url"] == got["images"][0]["thumb_url"]


@pytest.mark.parametrize(
    "fmt,ctype", [("JPEG", "image/jpeg"), ("PNG", "image/png"), ("WEBP", "image/webp")]
)
def test_jpeg_png_webp_are_accepted(pg_client, shop, fmt, ctype):
    _, headers, item = shop
    assert _upload(pg_client, headers, item, make_image(fmt), "a", ctype).status_code == 201


def test_an_untouched_12mp_phone_photo_is_accepted(pg_client, shop):
    _, headers, item = shop
    assert _upload(pg_client, headers, item, make_image("JPEG", (4032, 3024))).status_code == 201


def test_gif_is_refused(pg_client, shop, cloud, moderation):
    _, headers, item = shop
    r = _upload(pg_client, headers, item, make_image("GIF"), "a.gif", "image/gif")
    assert r.status_code == 422 and r.json()["code"] == "image_invalid"
    assert "GIF" in r.json()["message"]
    assert cloud.uploads == [] and moderation.await_count == 0


def test_a_text_file_is_refused(pg_client, shop):
    _, headers, item = shop
    r = _upload(pg_client, headers, item, b"not an image", "a.jpg", "image/jpeg")
    assert r.status_code == 422 and r.json()["code"] == "image_invalid"


def test_a_heic_photo_gets_a_clear_message(pg_client, shop):
    _, headers, item = shop
    heic = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic" + b"\x00" * 64
    r = _upload(pg_client, headers, item, heic, "IMG_1.HEIC", "image/heic")
    assert r.status_code == 422 and r.json()["code"] == "image_invalid"
    assert "HEIC" in r.json()["message"] and "JPEG" in r.json()["message"]


def test_over_5mb_is_413_image_too_large(pg_client, shop, cloud, moderation):
    _, headers, item = shop
    r = _upload(pg_client, headers, item, make_image("JPEG") + b"\x00" * (5 * 1024 * 1024))
    assert r.status_code == 413 and r.json()["code"] == "image_too_large"
    assert cloud.uploads == [] and moderation.await_count == 0


def test_moderation_block_returns_fixed_text_and_stores_nothing(
    pg_client, db_session, shop, cloud, moderation
):
    _, headers, item = shop
    moderation.return_value = (False, "violence detected")
    r = _upload(pg_client, headers, item)
    assert r.status_code == 422
    assert r.json() == {"code": "image_blocked", "message": BLOCKED_TEXT}  # never the reason
    assert cloud.uploads == [] and _photos(db_session, item) == []


def test_moderation_outage_fails_closed(pg_client, shop, cloud, moderation):
    _, headers, item = shop
    moderation.side_effect = RuntimeError("openai down")
    r = _upload(pg_client, headers, item)
    assert r.status_code == 422 and r.json()["code"] == "image_blocked"
    assert cloud.uploads == []


def test_cloudinary_down_is_503_and_no_row(pg_client, db_session, shop, cloud):
    _, headers, item = shop
    cloud.fail_upload = True
    r = _upload(pg_client, headers, item)
    assert r.status_code == 503
    assert r.json() == {
        "code": "image_storage_unavailable",
        "message": "Photo storage is unavailable right now. Try again in a moment.",
    }
    assert _photos(db_session, item) == []
    assert "secret" not in r.text


def test_cloudinary_not_configured_is_503(pg_client, shop, monkeypatch):
    from app.utils.cloudinary_client import cloudinary_manager

    _, headers, item = shop
    monkeypatch.setattr(cloudinary_manager, "enabled", False)
    r = _upload(pg_client, headers, item)
    assert r.status_code == 503 and r.json()["code"] == "image_storage_unavailable"


# --- cap ------------------------------------------------------------------------------------


def test_the_per_item_photo_cap_is_enforced_before_any_work_is_done(
    pg_client, db_session, shop, cloud, moderation
):
    tenant, headers, item = shop
    add_photos(db_session, tenant, item, 4)
    r = _upload(pg_client, headers, item)
    assert r.status_code == 409
    assert r.json() == {
        "code": "offering_image_limit",
        "message": "Your plan allows 4 photos per item. Remove one, or upgrade, to add more.",
        "limit": 4,
        "used": 4,
    }
    assert moderation.await_count == 0 and cloud.uploads == []


def test_higher_plans_allow_eight(pg_client, db_session, cloud, moderation):
    tenant = make_tenant(db_session, "professional")
    _, headers = make_user(db_session, tenant)
    add_items(db_session, tenant, 1)
    item = db_session.query(Offering).filter_by(tenant_id=tenant.id).one()
    add_photos(db_session, tenant, item, 7)
    assert _upload(pg_client, headers, item).status_code == 201
    assert _upload(pg_client, headers, item).status_code == 409


def test_a_race_that_fills_the_cap_during_upload_destroys_the_new_asset(
    pg_client, db_session, shop, cloud, moderation
):
    tenant, headers, item = shop
    add_photos(db_session, tenant, item, 3)

    async def fill_the_last_slot(*_args, **_kw):
        add_photos_at(db_session, tenant, item, position=3)
        return True, None

    moderation.side_effect = fill_the_last_slot
    r = _upload(pg_client, headers, item)
    assert r.status_code == 409 and r.json()["code"] == "offering_image_limit"
    assert len(cloud.uploads) == 1 and len(cloud.destroyed) == 1  # compensated, nothing leaked
    assert cloud.destroyed[0].startswith(f"luran/offerings/{tenant.id}/")
    assert len(_photos(db_session, item)) == 4


def add_photos_at(db, tenant, item, position):
    db.add(
        OfferingImage(
            offering_id=item.id,
            tenant_id=tenant.id,
            url="https://res.cloudinary.com/demo/image/upload/v1/raced.jpg",
            cloudinary_public_id="raced",
            position=position,
        )
    )
    db.commit()


# --- auth -----------------------------------------------------------------------------------


def test_only_a_tenant_admin_can_upload(pg_client, db_session, shop):
    tenant, _, item = shop
    _, agent = make_user(db_session, tenant, UserRole.SUPPORT_AGENT)
    assert _upload(pg_client, agent, item).status_code == 403


def test_another_tenants_item_is_404(pg_client, db_session, shop):
    _, _, item = shop
    other = make_tenant(db_session, "lite")
    _, other_headers = make_user(db_session, other)
    r = _upload(pg_client, other_headers, item)
    assert r.status_code == 404 and r.json()["code"] == "offering_not_found"


def test_a_lapsed_tenant_cannot_upload(pg_client, db_session, shop):
    from datetime import UTC, datetime, timedelta

    tenant, headers, item = shop
    tenant.subscription_status = "active"
    tenant.subscription_ends_at = datetime.now(UTC) - timedelta(days=1)
    db_session.commit()
    r = _upload(pg_client, headers, item)
    assert r.status_code == 403 and r.json()["code"] == "feature_not_in_plan"


# --- order ----------------------------------------------------------------------------------


def _ids(db, item):
    return [str(r.id) for r in _photos(db, item)]


def test_reorder_rewrites_positions_and_the_first_becomes_the_cover(pg_client, db_session, shop):
    tenant, headers, item = shop
    add_photos(db_session, tenant, item, 3)
    a, b, c = _ids(db_session, item)
    r = pg_client.put(
        f"{URL}/{item.id}/images/order", json={"image_ids": [c, a, b]}, headers=headers
    )
    assert r.status_code == 200, r.text
    assert [i["id"] for i in r.json()["data"]["images"]] == [c, a, b]
    assert [i["position"] for i in r.json()["data"]["images"]] == [0, 1, 2]
    assert _ids(db_session, item) == [c, a, b]


@pytest.mark.parametrize("mutate", ["missing", "extra", "duplicate", "foreign"])
def test_reorder_must_name_exactly_the_items_photos(pg_client, db_session, shop, mutate):
    tenant, headers, item = shop
    add_photos(db_session, tenant, item, 2)
    a, b = _ids(db_session, item)
    other = make_tenant(db_session, "lite")
    add_items(db_session, other, 1)
    other_item = db_session.query(Offering).filter_by(tenant_id=other.id).one()
    add_photos(db_session, other, other_item, 1)
    foreign = _ids(db_session, other_item)[0]
    ids = {
        "missing": [a],
        "extra": [a, b, "00000000-0000-0000-0000-000000000000"],
        "duplicate": [a, a],
        "foreign": [a, foreign],
    }[mutate]
    r = pg_client.put(f"{URL}/{item.id}/images/order", json={"image_ids": ids}, headers=headers)
    assert r.status_code == 422 and r.json()["code"] == "image_order_mismatch"
    assert _ids(db_session, item) == [a, b]


# --- delete one photo -----------------------------------------------------------------------


def test_deleting_the_cover_promotes_the_next_photo_and_renumbers(
    pg_client, db_session, shop, cloud
):
    tenant, headers, item = shop
    add_photos(db_session, tenant, item, 3)
    rows = _photos(db_session, item)
    cover_public_id = rows[0].cloudinary_public_id
    keep = [str(r.id) for r in rows[1:]]
    r = pg_client.delete(f"{URL}/{item.id}/images/{rows[0].id}", headers=headers)
    assert r.status_code == 204
    assert _ids(db_session, item) == keep
    assert [p.position for p in _photos(db_session, item)] == [0, 1]
    assert cloud.destroyed == [cover_public_id]  # the asset is destroyed, not just the row


def test_a_failed_asset_destroy_does_not_fail_the_delete(pg_client, db_session, shop, cloud):
    tenant, headers, item = shop
    add_photos(db_session, tenant, item, 1)
    photo = _photos(db_session, item)[0]
    cloud.fail_destroy = True
    r = pg_client.delete(f"{URL}/{item.id}/images/{photo.id}", headers=headers)
    assert r.status_code == 204 and _photos(db_session, item) == []  # the sweep reaps the asset


def test_deleting_an_unknown_or_foreign_photo_is_404(pg_client, db_session, shop):
    tenant, headers, item = shop
    r = pg_client.delete(
        f"{URL}/{item.id}/images/00000000-0000-0000-0000-000000000000", headers=headers
    )
    assert r.status_code == 404 and r.json()["code"] == "offering_image_not_found"


# --- hard delete of the item ----------------------------------------------------------------


def test_hard_delete_destroys_every_photo_asset(pg_client, db_session, shop, cloud):
    tenant, headers, item = shop
    add_photos(db_session, tenant, item, 3)
    public_ids = sorted(p.cloudinary_public_id for p in _photos(db_session, item))
    r = pg_client.delete(f"{URL}/{item.id}?hard=true", headers=headers)
    assert r.status_code == 204
    assert sorted(cloud.destroyed) == public_ids
    assert db_session.query(OfferingImage).filter_by(tenant_id=tenant.id).count() == 0


def test_hard_delete_survives_a_cloudinary_failure(pg_client, db_session, shop, cloud):
    tenant, headers, item = shop
    add_photos(db_session, tenant, item, 2)
    cloud.fail_destroy = True
    assert pg_client.delete(f"{URL}/{item.id}?hard=true", headers=headers).status_code == 204
    assert db_session.query(Offering).filter_by(tenant_id=tenant.id).count() == 0


def test_soft_delete_keeps_the_photos(pg_client, db_session, shop, cloud):
    tenant, headers, item = shop
    add_photos(db_session, tenant, item, 2)
    assert pg_client.delete(f"{URL}/{item.id}", headers=headers).status_code == 204
    assert cloud.destroyed == [] and len(_photos(db_session, item)) == 2


def test_body_helper_is_still_valid_for_create(pg_client, db_session, shop):
    _, headers, _ = shop
    assert pg_client.post(URL, json=body(name="Another"), headers=headers).status_code == 201
