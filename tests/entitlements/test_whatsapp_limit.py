"""WhatsApp: the grant stays a platform decision, the number of lines comes from the plan."""

from datetime import UTC, datetime

from app.api.deps import get_current_user
from app.db.models.user import UserRole
from app.db.models.whatsapp import WhatsAppConnection
from app.main import app
from app.schemas.user import UserResponse
from tests.entitlements.helpers import caps_of, make_tenant, make_user
from tests.whatsapp.conftest import (  # noqa: F401  (fixtures)
    PHONE_A,
    PHONE_B,
    PHONE_C,
    consent_body,
    fake_sidecar,
    queue,
    wa_settings,
)

URL = "/api/v1/whatsapp/connections"


def _login(db, tenant):
    user, _ = make_user(db, tenant)
    now = datetime.now(UTC)
    app.dependency_overrides[get_current_user] = lambda: UserResponse(
        id=user.id,
        tenant_id=tenant.id,
        email=user.email,
        full_name=user.full_name,
        role=UserRole.TENANT_ADMIN,
        is_active=True,
        created_at=now,
        updated_at=now,
    )


def _count(db, tenant):
    db.expire_all()
    return db.query(WhatsAppConnection).filter_by(tenant_id=tenant.id).count()


def test_lite_gets_one_number_even_though_nothing_was_set_by_hand(pg_client, db_session, fake_sidecar, queue):
    tenant = make_tenant(db_session, "lite", whatsapp_grant=True)
    _login(db_session, tenant)
    assert pg_client.post(URL, json=consent_body(PHONE_A)).status_code == 201
    second = pg_client.post(URL, json=consent_body(PHONE_B))
    assert second.status_code == 409 and second.json()["code"] == "connection_limit"
    assert "1 WhatsApp number" in second.json()["message"]
    assert _count(db_session, tenant) == 1


def test_professional_gets_two_and_growth_five(db_session, pg_client, fake_sidecar, queue):
    pro = make_tenant(db_session, "professional", whatsapp_grant=True)
    _login(db_session, pro)
    assert pg_client.post(URL, json=consent_body(PHONE_A)).status_code == 201
    assert pg_client.post(URL, json=consent_body(PHONE_B)).status_code == 201
    third = pg_client.post(URL, json=consent_body(PHONE_C))
    assert third.status_code == 409 and third.json()["code"] == "connection_limit"


def test_enterprise_has_no_plan_limit(db_session, pg_client, fake_sidecar, queue):
    tenant = make_tenant(db_session, "enterprise", whatsapp_grant=True)
    _login(db_session, tenant)
    first = pg_client.post(URL, json=consent_body(PHONE_A))
    assert first.status_code == 201, first.text
    assert pg_client.post(URL, json=consent_body(PHONE_B)).status_code == 201


def test_without_the_grant_the_plan_alone_does_not_open_whatsapp(db_session, pg_client, fake_sidecar, queue):
    tenant = make_tenant(db_session, "growth", whatsapp_grant=False)
    _login(db_session, tenant)
    r = pg_client.post(URL, json=consent_body(PHONE_A))
    assert r.status_code == 403 and r.json()["code"] == "whatsapp_not_enabled"


def test_a_hand_set_number_overrides_the_plan(db_session, pg_client, fake_sidecar, queue):
    tenant = make_tenant(db_session, "lite", whatsapp_grant=True)
    caps = caps_of(db_session, tenant)
    caps.max_whatsapp_numbers = 2
    caps.override_flags = {"max_whatsapp_numbers": True}
    db_session.commit()
    _login(db_session, tenant)
    assert pg_client.post(URL, json=consent_body(PHONE_A)).status_code == 201
    assert pg_client.post(URL, json=consent_body(PHONE_B)).status_code == 201
