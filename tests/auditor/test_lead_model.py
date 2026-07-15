"""Tests for Lead model — PII encryption, email_hash lookup, LeadStatus enum."""


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


def test_lead_email_hash_auto_derived_on_insert_and_update(db):
    """Listener sets email_hash automatically; caller must NOT supply it manually."""
    # --- insert: email_hash must be derived without caller setting it ---
    lead = Lead(
        tenant_id=None,
        email="Test.User@Example.COM",
        source="public_auditor",
        utm={},
        status=LeadStatus.NEW,
    )
    db.add(lead)
    db.flush()
    db.refresh(lead)
    assert lead.email_hash == Lead.compute_email_hash("Test.User@Example.COM"), (
        "before_insert listener must auto-derive email_hash"
    )

    # --- update: changing email must update email_hash automatically ---
    lead.email = "Updated.User@Example.COM"
    db.flush()
    db.refresh(lead)
    assert lead.email_hash == Lead.compute_email_hash("Updated.User@Example.COM"), (
        "before_update listener must re-derive email_hash when email changes"
    )
