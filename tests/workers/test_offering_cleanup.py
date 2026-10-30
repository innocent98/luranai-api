"""Orphan sweep: Cloudinary offering assets with no row, older than a day, are destroyed."""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from app.db.models.offering import Offering, OfferingImage
from app.workers.offering_cleanup import sweep_orphan_offering_images
from tests.entitlements.helpers import make_tenant
from tests.offerings.helpers import add_items, add_photos

LIST = "app.workers.offering_cleanup.cloudinary_manager.list_tagged_images"
DESTROY = "app.workers.offering_cleanup.cloudinary_manager.destroy"


def _asset(public_id, age_hours):
    created = datetime.now(UTC) - timedelta(hours=age_hours)
    return {"public_id": public_id, "created_at": created.strftime("%Y-%m-%dT%H:%M:%SZ")}


@pytest.fixture
def session_local(db_session, monkeypatch):
    """The worker opens its own session: hand it the test's transactional one."""
    monkeypatch.setattr("app.workers.offering_cleanup.SessionLocal", lambda: db_session)
    monkeypatch.setattr(db_session, "close", lambda: None)


async def test_old_unreferenced_assets_are_destroyed_and_the_rest_kept(db_session, session_local):
    tenant = make_tenant(db_session, "lite")
    add_items(db_session, tenant, 1)
    item = db_session.query(Offering).filter_by(tenant_id=tenant.id).one()
    add_photos(db_session, tenant, item, 1)
    kept_id = (
        db_session.query(OfferingImage).filter_by(offering_id=item.id).one().cloudinary_public_id
    )
    assets = [
        _asset("luran/offerings/t/orphan-old", 30),
        _asset("luran/offerings/t/orphan-new", 2),  # may be mid-upload: leave it
        _asset(kept_id, 100),  # has a row: leave it
    ]
    with patch(LIST, return_value=(assets, None)), patch(DESTROY, return_value=(True, None)) as d:
        destroyed = await sweep_orphan_offering_images({})
    assert destroyed == 1
    assert [c.args[0] for c in d.call_args_list] == ["luran/offerings/t/orphan-old"]


async def test_it_pages_through_every_result(db_session, session_local):
    pages = [([_asset("a", 48)], "c2"), ([_asset("b", 48)], None)]
    with patch(LIST, side_effect=pages), patch(DESTROY, return_value=(True, None)) as d:
        destroyed = await sweep_orphan_offering_images({})
    assert destroyed == 2 and d.call_count == 2


async def test_a_failed_destroy_does_not_stop_the_batch(db_session, session_local):
    assets = [_asset("a", 48), _asset("b", 48)]
    with (
        patch(LIST, return_value=(assets, None)),
        patch(DESTROY, side_effect=[(False, "x"), (True, None)]),
    ):
        assert await sweep_orphan_offering_images({}) == 1


async def test_a_listing_error_is_swallowed_and_returns_zero(db_session, session_local):
    with patch(LIST, side_effect=RuntimeError("api down")):
        assert await sweep_orphan_offering_images({}) == 0


def test_the_sweep_is_registered_with_the_worker_as_a_daily_cron():
    from app.workers.document_processor import WorkerSettings

    assert sweep_orphan_offering_images in WorkerSettings.functions
    jobs = [c for c in WorkerSettings.cron_jobs if c.coroutine is sweep_orphan_offering_images]
    assert len(jobs) == 1 and jobs[0].hour == {3} and jobs[0].minute == 45
