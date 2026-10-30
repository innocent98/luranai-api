"""Shared builders for the offerings tests (Postgres session with the seeded plans)."""

from uuid import uuid4

from app.db.models.offering import (
    Offering,
    OfferingAvailability,
    OfferingImage,
    OfferingKind,
    OfferingPriceMode,
    OfferingStatus,
    TenantOfferingsSettings,
)

URL = "/api/v1/offerings"


def body(**kw):
    values = {
        "kind": "product",
        "name": "Canvas sneaker",
        "price_mode": "fixed",
        "price": 12000,
        "availability": "in_stock",
    }
    values.update(kw)
    return values


def add_items(db, tenant, n, *, status=OfferingStatus.ACTIVE, start=1, **kw):
    """Insert `n` items straight into the database (fast path for cap tests)."""
    for i in range(start, start + n):
        values = dict(
            tenant_id=tenant.id,
            seq=i,
            kind=OfferingKind.PRODUCT,
            name=f"Item {i}",
            price_mode=OfferingPriceMode.FIXED,
            price=1000,
            availability=OfferingAvailability.IN_STOCK,
            status=status,
        )
        values.update(kw)
        db.add(Offering(**values))
    settings = db.query(TenantOfferingsSettings).filter_by(tenant_id=tenant.id).one_or_none()
    if settings is None:
        db.add(TenantOfferingsSettings(tenant_id=tenant.id, next_seq=start + n))
    else:
        settings.next_seq = max(settings.next_seq, start + n)
    db.commit()


def add_photos(db, tenant, item, n):
    for pos in range(n):
        db.add(
            OfferingImage(
                offering_id=item.id,
                tenant_id=tenant.id,
                url=f"https://res.cloudinary.com/demo/image/upload/v1/luran/offerings/{uuid4().hex}.jpg",
                cloudinary_public_id=f"luran/offerings/{uuid4().hex}",
                position=pos,
                width=1200,
                height=900,
                bytes=240_000,
            )
        )
    db.commit()
