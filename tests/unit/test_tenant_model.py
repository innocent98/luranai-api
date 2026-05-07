from app.db.models.tenant import Tenant


def test_tenant_has_nullable_category_column():
    col = Tenant.__table__.columns["category"]
    assert col.nullable is True
    assert col.type.length == 40  # VARCHAR(40)


def test_tenant_category_defaults_to_none(db):
    tenant = Tenant(
        name="test_category_default",
        display_name="Test Co",
        api_key_hash="hash_abc123",
    )
    db.add(tenant)
    db.commit()
    db.refresh(tenant)
    assert tenant.category is None


def test_tenant_category_accepts_string_value(db):
    tenant = Tenant(
        name="test_category_set",
        display_name="Retail Co",
        api_key_hash="hash_def456",
        category="retail_ecommerce",
    )
    db.add(tenant)
    db.commit()
    db.refresh(tenant)
    assert tenant.category == "retail_ecommerce"
