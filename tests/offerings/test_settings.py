"""Confirm-today, settings, the Not-found (misses) list and the 90-day miss sweep."""

from datetime import UTC, datetime, timedelta

import pytest

from app.db.models.offering import (
    OfferingActivityDay,
    OfferingMiss,
    TenantOfferingsSettings,
)
from app.db.models.sales_agent_config import TenantSalesAgentConfig
from app.db.models.user import UserRole
from app.workers.offering_cleanup import sweep_old_offering_misses
from tests.entitlements.helpers import make_tenant, make_user
from tests.offerings.helpers import URL

SETTINGS = f"{URL}/settings"
MISSES = f"{URL}/misses"
CONFIRM = f"{URL}/confirm"


@pytest.fixture
def shop(db_session):
    tenant = make_tenant(db_session, "lite")
    _, headers = make_user(db_session, tenant)
    return tenant, headers


def _miss(db, tenant, term, count=1, days_ago=0):
    row = OfferingMiss(
        tenant_id=tenant.id,
        term=term,
        count=count,
        first_seen_at=datetime.now(UTC) - timedelta(days=days_ago + 1),
        last_seen_at=datetime.now(UTC) - timedelta(days=days_ago),
    )
    db.add(row)
    db.commit()
    return row


# --- confirm --------------------------------------------------------------------------------


def test_confirm_stamps_the_time_and_records_an_activity_day(pg_client, db_session, shop):
    tenant, headers = shop
    before = datetime.now(UTC)
    r = pg_client.post(CONFIRM, headers=headers)
    assert r.status_code == 200, r.text
    stamp = datetime.fromisoformat(r.json()["data"]["confirmed_at"])
    assert before - timedelta(seconds=1) <= stamp <= datetime.now(UTC) + timedelta(seconds=1)
    row = db_session.query(TenantOfferingsSettings).filter_by(tenant_id=tenant.id).one()
    assert row.confirmed_at is not None
    days = db_session.query(OfferingActivityDay).filter_by(tenant_id=tenant.id).all()
    assert [d.day for d in days] == [datetime.now(UTC).date()]
    assert (
        pg_client.get(SETTINGS, headers=headers).json()["data"]["confirmed_at"]
        == r.json()["data"]["confirmed_at"]
    )


def test_confirm_needs_an_admin_and_an_active_plan(pg_client, db_session, shop):
    tenant, headers = shop
    _, agent = make_user(db_session, tenant, UserRole.SUPPORT_AGENT)
    assert pg_client.post(CONFIRM, headers=agent).status_code == 403
    assert pg_client.post(CONFIRM).status_code == 401
    tenant.subscription_status = "active"
    tenant.subscription_ends_at = datetime.now(UTC) - timedelta(days=1)
    db_session.commit()
    r = pg_client.post(CONFIRM, headers=headers)
    assert r.status_code == 403 and r.json()["code"] == "feature_not_in_plan"


# --- settings -------------------------------------------------------------------------------


def test_settings_default_without_creating_a_row(pg_client, db_session, shop):
    tenant, headers = shop
    r = pg_client.get(SETTINGS, headers=headers)
    assert r.status_code == 200
    assert r.json() == {
        "data": {"enabled": True, "currency": "NGN", "stale_after_days": 7, "confirmed_at": None}
    }
    assert db_session.query(TenantOfferingsSettings).filter_by(tenant_id=tenant.id).count() == 0


def test_settings_currency_defaults_to_the_sales_config_currency(pg_client, db_session, shop):
    tenant, headers = shop
    cfg = TenantSalesAgentConfig.default_for(tenant.id)
    cfg.currency = "USD"
    db_session.add(cfg)
    db_session.commit()
    assert pg_client.get(SETTINGS, headers=headers).json()["data"]["currency"] == "USD"
    assert (
        pg_client.patch(SETTINGS, json={"enabled": False}, headers=headers).json()["data"][
            "currency"
        ]
        == "USD"
    )


def test_patch_changes_only_the_given_fields(pg_client, shop):
    _, headers = shop
    r = pg_client.patch(SETTINGS, json={"stale_after_days": 3}, headers=headers)
    assert r.status_code == 200
    assert r.json()["data"] == {
        "enabled": True,
        "currency": "NGN",
        "stale_after_days": 3,
        "confirmed_at": None,
    }
    r = pg_client.patch(SETTINGS, json={"enabled": False, "currency": "ghs"}, headers=headers)
    assert r.json()["data"] == {
        "enabled": False,
        "currency": "GHS",
        "stale_after_days": 3,
        "confirmed_at": None,
    }
    assert pg_client.get(SETTINGS, headers=headers).json()["data"]["enabled"] is False


@pytest.mark.parametrize(
    "payload,code",
    [
        ({"stale_after_days": -1}, "stale_after_days_invalid"),
        ({"stale_after_days": 366}, "stale_after_days_invalid"),
        ({"currency": "NAIRA"}, "currency_invalid"),
        ({"currency": "N1"}, "currency_invalid"),
        ({"currency": ""}, "currency_invalid"),
    ],
)
def test_settings_validation(pg_client, shop, payload, code):
    _, headers = shop
    r = pg_client.patch(SETTINGS, json=payload, headers=headers)
    assert r.status_code == 422 and r.json()["code"] == "validation"
    assert r.json()["errors"][0]["code"] == code


def test_zero_means_never_caveat_and_is_accepted(pg_client, shop):
    _, headers = shop
    r = pg_client.patch(SETTINGS, json={"stale_after_days": 0}, headers=headers)
    assert r.status_code == 200 and r.json()["data"]["stale_after_days"] == 0


def test_settings_are_per_tenant_and_patch_needs_an_admin(pg_client, db_session, shop):
    tenant, headers = shop
    pg_client.patch(SETTINGS, json={"enabled": False}, headers=headers)
    other = make_tenant(db_session, "lite")
    _, other_headers = make_user(db_session, other)
    assert pg_client.get(SETTINGS, headers=other_headers).json()["data"]["enabled"] is True
    _, agent = make_user(db_session, tenant, UserRole.SUPPORT_AGENT)
    assert pg_client.get(SETTINGS, headers=agent).status_code == 200
    assert pg_client.patch(SETTINGS, json={"enabled": True}, headers=agent).status_code == 403


# --- misses ---------------------------------------------------------------------------------


def test_misses_are_listed_by_demand_for_the_window(pg_client, db_session, shop):
    tenant, headers = shop
    _miss(db_session, tenant, "red heels", count=5, days_ago=1)
    _miss(db_session, tenant, "gym bag", count=9, days_ago=2)
    _miss(db_session, tenant, "old thing", count=50, days_ago=40)  # outside the 30-day default
    r = pg_client.get(MISSES, headers=headers)
    assert r.status_code == 200
    data = r.json()["data"]
    assert [(m["term"], m["count"]) for m in data] == [("gym bag", 9), ("red heels", 5)]
    assert set(data[0]) == {"id", "term", "count", "last_seen_at"}  # nothing identifying
    wide = pg_client.get(MISSES, params={"days": 90}, headers=headers).json()["data"]
    assert [m["term"] for m in wide] == ["old thing", "gym bag", "red heels"]


@pytest.mark.parametrize("days", [0, 91, "x"])
def test_misses_window_is_bounded(pg_client, shop, days):
    _, headers = shop
    assert pg_client.get(MISSES, params={"days": days}, headers=headers).status_code == 422


def test_misses_never_cross_tenants(pg_client, db_session, shop):
    tenant, headers = shop
    other = make_tenant(db_session, "lite")
    _miss(db_session, other, "secret term")
    assert pg_client.get(MISSES, headers=headers).json()["data"] == []


def test_dismissing_a_miss(pg_client, db_session, shop):
    tenant, headers = shop
    keep, gone = _miss(db_session, tenant, "keep"), _miss(db_session, tenant, "gone")
    keep_id, gone_id = str(keep.id), str(gone.id)
    assert pg_client.delete(f"{MISSES}/{gone_id}", headers=headers).status_code == 204
    assert [m["id"] for m in pg_client.get(MISSES, headers=headers).json()["data"]] == [keep_id]
    r = pg_client.delete(f"{MISSES}/{gone_id}", headers=headers)
    assert r.status_code == 404 and r.json() == {
        "code": "offering_miss_not_found",
        "message": "Not-found term not found",
    }


def test_one_tenant_cannot_dismiss_anothers_miss_and_agents_cannot_dismiss(
    pg_client, db_session, shop
):
    tenant, headers = shop
    other = make_tenant(db_session, "lite")
    _, other_headers = make_user(db_session, other)
    row = _miss(db_session, other, "theirs")
    assert pg_client.delete(f"{MISSES}/{row.id}", headers=headers).status_code == 404
    _, agent = make_user(db_session, other, UserRole.SUPPORT_AGENT)
    assert pg_client.delete(f"{MISSES}/{row.id}", headers=agent).status_code == 403
    assert pg_client.get(MISSES, headers=agent).status_code == 200
    assert len(pg_client.get(MISSES, headers=other_headers).json()["data"]) == 1


# --- 90-day sweep ---------------------------------------------------------------------------


@pytest.fixture
def session_local(db_session, monkeypatch):
    monkeypatch.setattr("app.workers.offering_cleanup.SessionLocal", lambda: db_session)
    monkeypatch.setattr(db_session, "close", lambda: None)


async def test_the_sweep_deletes_terms_not_seen_for_90_days(db_session, session_local):
    tenant = make_tenant(db_session, "lite")
    _miss(db_session, tenant, "ancient", days_ago=91)
    _miss(db_session, tenant, "edge", days_ago=89)
    _miss(db_session, tenant, "fresh", days_ago=1)
    deleted = await sweep_old_offering_misses({})
    assert deleted == 1
    left = {m.term for m in db_session.query(OfferingMiss).filter_by(tenant_id=tenant.id)}
    assert left == {"edge", "fresh"}


def test_the_miss_sweep_is_registered_daily():
    from app.workers.document_processor import WorkerSettings

    assert sweep_old_offering_misses in WorkerSettings.functions
    jobs = [c for c in WorkerSettings.cron_jobs if c.coroutine is sweep_old_offering_misses]
    assert len(jobs) == 1 and jobs[0].hour == {3}
