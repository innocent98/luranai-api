"""Tests for BusinessProfile model."""

import uuid

from app.db.models.business_profile import BusinessProfile, BusinessProfileSource


def test_business_profile_persists(db):
    bp = BusinessProfile(
        tenant_id=uuid.uuid4(),
        summary="A Lagos fintech",
        industry="fintech",
        offerings=["wallet", "bills"],
        audience=["SMEs"],
        positioning="fast and local",
        key_entities=["LuranPay"],
        tone={"style": "warm"},
        source=BusinessProfileSource.AUDITOR,
        confidence=0.82,
        model_version="gpt-4o",
        raw={"k": "v"},
    )
    db.add(bp)
    db.flush()
    db.refresh(bp)
    assert bp.id is not None
    assert bp.source.value == "auditor"
    assert bp.offerings == ["wallet", "bills"]
