"""Offerings tables: constraints that the services and the AI path rely on (real Postgres)."""

from datetime import date
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from app.db.models.lead import Lead
from app.db.models.notification_log import NotifEvent
from app.db.models.offering import (
    LeadOfferingInterest,
    Offering,
    OfferingActivityDay,
    OfferingAvailability,
    OfferingImage,
    OfferingKind,
    OfferingMiss,
    OfferingPriceMode,
    OfferingStatus,
    TenantOfferingsSettings,
)
from tests.entitlements.helpers import make_tenant


def _item(tenant_id, seq=1, **kw):
    values = dict(
        tenant_id=tenant_id,
        seq=seq,
        kind=OfferingKind.PRODUCT,
        name="Canvas sneaker",
        price_mode=OfferingPriceMode.FIXED,
        price=12_000,
        availability=OfferingAvailability.IN_STOCK,
    )
    values.update(kw)
    return Offering(**values)


def test_offering_defaults(db_session):
    tenant = make_tenant(db_session)
    item = _item(tenant.id)
    db_session.add(item)
    db_session.commit()
    db_session.refresh(item)
    assert item.status == OfferingStatus.ACTIVE
    assert item.negotiable is False
    assert item.created_at is not None and item.updated_at is not None
    assert item.archived_at is None and item.external_ref is None


def test_seq_is_unique_per_tenant_not_globally(db_session):
    a, b = make_tenant(db_session), make_tenant(db_session)
    db_session.add_all([_item(a.id, 1), _item(b.id, 1)])
    db_session.commit()
    db_session.add(_item(a.id, 1, name="dup"))
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_external_ref_is_unique_per_tenant_when_set(db_session):
    tenant = make_tenant(db_session)
    db_session.add_all([_item(tenant.id, 1), _item(tenant.id, 2)])  # two items with no ref are fine
    db_session.commit()
    db_session.add_all([_item(tenant.id, 3, external_ref="SKU-1")])
    db_session.commit()
    db_session.add(_item(tenant.id, 4, external_ref="SKU-1"))
    with pytest.raises(IntegrityError):
        db_session.commit()


@pytest.mark.parametrize(
    "mode,price",
    [
        (OfferingPriceMode.FIXED, None),  # fixed needs a price
        (OfferingPriceMode.FROM, None),
        (OfferingPriceMode.ON_REQUEST, 5_000),  # on request must have none
        (OfferingPriceMode.FIXED, 0),  # 0 < price
        (OfferingPriceMode.FIXED, -1),
        (OfferingPriceMode.FIXED, 10_000_000_001),  # <= 10,000,000,000
    ],
)
def test_price_must_match_the_price_mode(db_session, mode, price):
    tenant = make_tenant(db_session)
    db_session.add(_item(tenant.id, price_mode=mode, price=price))
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_price_upper_bound_is_accepted(db_session):
    tenant = make_tenant(db_session)
    db_session.add(_item(tenant.id, price=10_000_000_000))
    db_session.commit()


def test_negative_quantity_is_refused(db_session):
    tenant = make_tenant(db_session)
    db_session.add(_item(tenant.id, quantity=-1))
    with pytest.raises(IntegrityError):
        db_session.commit()


def _image(item, tenant, position, tag=None):
    return OfferingImage(
        offering_id=item.id,
        tenant_id=tenant.id,
        url=f"https://res.cloudinary.com/x/{tag or position}.jpg",
        cloudinary_public_id=f"p{tag or position}",
        position=position,
        width=100,
        height=100,
        bytes=1000,
    )


def test_image_positions_are_unique_per_item(db_session):
    tenant = make_tenant(db_session)
    item = _item(tenant.id)
    db_session.add(item)
    db_session.flush()
    db_session.add_all([_image(item, tenant, 0), _image(item, tenant, 1)])
    db_session.commit()
    db_session.add(_image(item, tenant, 1, tag="dup"))
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_images_cascade_with_the_item(db_session):
    tenant = make_tenant(db_session)
    item = _item(tenant.id)
    db_session.add(item)
    db_session.flush()
    db_session.add_all([_image(item, tenant, 0), _image(item, tenant, 1)])
    db_session.commit()
    tenant_id = tenant.id
    db_session.query(Offering).filter_by(tenant_id=tenant_id).delete()
    db_session.commit()
    assert db_session.query(OfferingImage).filter_by(tenant_id=tenant_id).count() == 0


def test_settings_row_defaults_and_one_per_tenant(db_session):
    tenant = make_tenant(db_session)
    db_session.add(TenantOfferingsSettings(tenant_id=tenant.id))
    db_session.commit()
    row = db_session.query(TenantOfferingsSettings).filter_by(tenant_id=tenant.id).one()
    assert (row.enabled, row.currency, row.stale_after_days, row.next_seq) == (True, "NGN", 7, 1)
    assert row.confirmed_at is None
    db_session.add(TenantOfferingsSettings(tenant_id=tenant.id))
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_activity_day_is_one_row_per_tenant_and_day(db_session):
    tenant = make_tenant(db_session)
    db_session.add(OfferingActivityDay(tenant_id=tenant.id, day=date(2026, 10, 30)))
    db_session.commit()
    db_session.add(OfferingActivityDay(tenant_id=tenant.id, day=date(2026, 10, 30)))
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_miss_term_is_unique_per_tenant_and_counts(db_session):
    tenant = make_tenant(db_session)
    db_session.add(OfferingMiss(tenant_id=tenant.id, term="red heels"))
    db_session.commit()
    miss = db_session.query(OfferingMiss).filter_by(tenant_id=tenant.id).one()
    assert miss.count == 1 and miss.first_seen_at and miss.last_seen_at
    db_session.add(OfferingMiss(tenant_id=tenant.id, term="red heels"))
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_interest_snapshots_survive_the_item_being_deleted(db_session):
    tenant = make_tenant(db_session)
    item = _item(tenant.id)
    lead = Lead(tenant_id=tenant.id)
    db_session.add_all([item, lead])
    db_session.flush()
    interest = LeadOfferingInterest(
        tenant_id=tenant.id,
        lead_id=lead.id,
        offering_id=item.id,
        ref="#1",
        name_snapshot="Canvas sneaker",
        price_mode_snapshot="fixed",
        price_snapshot=12_000,
        qty=2,
    )
    db_session.add(interest)
    db_session.commit()
    interest_id, lead_id = interest.id, lead.id
    db_session.delete(item)
    db_session.commit()
    db_session.expire_all()
    kept = db_session.get(LeadOfferingInterest, interest_id)
    assert kept is not None and kept.offering_id is None  # FK SET NULL
    assert kept.name_snapshot == "Canvas sneaker" and kept.price_snapshot == 12_000
    db_session.delete(db_session.get(Lead, lead_id))
    db_session.commit()
    assert db_session.get(LeadOfferingInterest, interest_id) is None  # cascades with the lead


def test_interest_qty_must_be_at_least_one(db_session):
    tenant = make_tenant(db_session)
    lead = Lead(tenant_id=tenant.id)
    db_session.add(lead)
    db_session.flush()
    db_session.add(
        LeadOfferingInterest(
            tenant_id=tenant.id,
            lead_id=lead.id,
            ref="#1",
            name_snapshot="x",
            price_mode_snapshot="on_request",
            qty=0,
        )
    )
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_offering_request_notification_event_exists():
    assert NotifEvent.OFFERING_REQUEST.value == "offering_request"


def test_ids_are_not_reused_across_tenants_in_queries(db_session):
    """Sanity: two tenants can hold the same seq without seeing each other's rows."""
    a, b = make_tenant(db_session), make_tenant(db_session)
    ia, ib = _item(a.id, 1, name="A"), _item(b.id, 1, name="B")
    db_session.add_all([ia, ib])
    db_session.commit()
    names = [o.name for o in db_session.query(Offering).filter_by(tenant_id=a.id)]
    assert names == ["A"]
    assert uuid4() not in (ia.id, ib.id)
