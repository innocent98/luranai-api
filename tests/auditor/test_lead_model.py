"""Tests for Lead model — PII encryption, email_hash lookup, LeadStatus enum."""

import uuid

from app.db.models.lead import Lead, LeadStatus


def test_lead_persists_with_encrypted_pii_and_hash(db):
    h = Lead.compute_email_hash("Owner@Example.com")
    lead = Lead(
        tenant_id=None,
        name="Ada Owner",
        email="Owner@Example.com",
        email_hash=h,
        source="public_auditor",
        utm={"utm_source": "x"},
        status=LeadStatus.NEW,
    )
    db.add(lead)
    db.flush()
    db.refresh(lead)
    assert lead.id is not None
    assert lead.email == "Owner@Example.com"  # decrypts transparently
    assert lead.email_hash == Lead.compute_email_hash("owner@example.com")
    assert lead.status.value == "new"
