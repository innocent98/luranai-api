"""Lead detail gains an additive `interests[]` (what the customer chose, as they saw it)."""

from datetime import UTC, datetime, timedelta

from app.db.models.lead import Lead
from app.db.models.offering import LeadOfferingInterest, Offering
from tests.entitlements.helpers import make_tenant, make_user
from tests.offerings.helpers import add_items

LEADS = "/api/v1/leads"
INTEREST_KEYS = {"ref", "name", "price_mode", "price", "price_note", "qty", "offered_price"}


def _lead(db, tenant):
    lead = Lead(tenant_id=tenant.id, name="Ada", email="ada@example.com")
    db.add(lead)
    db.commit()
    return lead


def _interest(
    db,
    tenant,
    lead,
    ref,
    name,
    *,
    price=12000,
    mode="fixed",
    qty=1,
    offered=None,
    offering=None,
    age=0,
):
    row = LeadOfferingInterest(
        tenant_id=tenant.id,
        lead_id=lead.id,
        offering_id=offering.id if offering else None,
        ref=ref,
        name_snapshot=name,
        price_mode_snapshot=mode,
        price_snapshot=price,
        price_note_snapshot="per pair" if mode == "fixed" else None,
        qty=qty,
        offered_price=offered,
        created_at=datetime.now(UTC) - timedelta(minutes=age),
    )
    db.add(row)
    db.commit()
    return row


def test_a_lead_with_no_interests_has_an_empty_list(pg_client, db_session):
    tenant = make_tenant(db_session, "lite")
    _, headers = make_user(db_session, tenant)
    lead = _lead(db_session, tenant)
    r = pg_client.get(f"{LEADS}/{lead.id}", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["data"]["interests"] == []


def test_interests_are_listed_oldest_first_with_the_snapshots(pg_client, db_session):
    tenant = make_tenant(db_session, "lite")
    _, headers = make_user(db_session, tenant)
    lead = _lead(db_session, tenant)
    _interest(db_session, tenant, lead, "#2", "Tote bag", price=8500, qty=1, age=1)
    _interest(db_session, tenant, lead, "#1", "Canvas sneaker", qty=2, offered=10000, age=5)
    data = pg_client.get(f"{LEADS}/{lead.id}", headers=headers).json()["data"]
    assert data["interests"] == [
        {
            "ref": "#1",
            "name": "Canvas sneaker",
            "price_mode": "fixed",
            "price": 12000,
            "price_note": "per pair",
            "qty": 2,
            "offered_price": 10000,
        },
        {
            "ref": "#2",
            "name": "Tote bag",
            "price_mode": "fixed",
            "price": 8500,
            "price_note": "per pair",
            "qty": 1,
            "offered_price": None,
        },
    ]
    assert set(data["interests"][0]) == INTEREST_KEYS


def test_the_snapshot_survives_the_item_being_edited_or_deleted(pg_client, db_session):
    tenant = make_tenant(db_session, "lite")
    _, headers = make_user(db_session, tenant)
    add_items(db_session, tenant, 1, name="Canvas sneaker", price=12000)
    item = db_session.query(Offering).filter_by(tenant_id=tenant.id).one()
    lead = _lead(db_session, tenant)
    _interest(db_session, tenant, lead, "#1", "Canvas sneaker", price=12000, offering=item)
    item.name, item.price = "Renamed", 99999
    db_session.commit()
    first = pg_client.get(f"{LEADS}/{lead.id}", headers=headers).json()["data"]["interests"][0]
    assert first["name"] == "Canvas sneaker" and first["price"] == 12000
    db_session.delete(item)
    db_session.commit()
    again = pg_client.get(f"{LEADS}/{lead.id}", headers=headers).json()["data"]["interests"][0]
    assert again == first


def test_other_tenants_interest_rows_never_appear(pg_client, db_session):
    tenant = make_tenant(db_session, "lite")
    other = make_tenant(db_session, "lite")
    _, headers = make_user(db_session, tenant)
    lead = _lead(db_session, tenant)
    _interest(db_session, other, lead, "#9", "Foreign row")  # wrong tenant_id on purpose
    assert pg_client.get(f"{LEADS}/{lead.id}", headers=headers).json()["data"]["interests"] == []


def test_the_patch_and_score_responses_carry_interests_too(pg_client, db_session):
    tenant = make_tenant(db_session, "lite")
    _, headers = make_user(db_session, tenant)
    lead = _lead(db_session, tenant)
    _interest(db_session, tenant, lead, "#1", "Canvas sneaker")
    patched = pg_client.patch(f"{LEADS}/{lead.id}", json={"status": "contacted"}, headers=headers)
    assert patched.status_code == 200 and len(patched.json()["data"]["interests"]) == 1
    scored = pg_client.post(f"{LEADS}/{lead.id}/score", headers=headers)
    assert scored.status_code == 200 and len(scored.json()["data"]["interests"]) == 1


def test_existing_detail_fields_are_unchanged(pg_client, db_session):
    tenant = make_tenant(db_session, "lite")
    _, headers = make_user(db_session, tenant)
    lead = _lead(db_session, tenant)
    data = pg_client.get(f"{LEADS}/{lead.id}", headers=headers).json()["data"]
    assert {
        "id",
        "name",
        "company",
        "intent",
        "lead_score",
        "tier",
        "status",
        "source_label",
        "created_at",
        "email",
        "team_size",
        "industry",
        "conversation_id",
        "score_breakdown",
        "source",
        "meeting",
        "quote",
        "crm",
    } <= data.keys()
