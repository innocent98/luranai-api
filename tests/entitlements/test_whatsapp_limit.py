"""WhatsApp: on by plan for every tenant, the number of lines comes from the plan, an admin can override."""

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


def test_lite_gets_one_number_even_though_nothing_was_set_by_hand(
    pg_client, db_session, fake_sidecar, queue
):
    tenant = make_tenant(db_session, "lite")
    _login(db_session, tenant)
    assert pg_client.post(URL, json=consent_body(PHONE_A)).status_code == 201
    second = pg_client.post(URL, json=consent_body(PHONE_B))
    assert second.status_code == 409 and second.json()["code"] == "connection_limit"
    assert "1 WhatsApp number" in second.json()["message"]
    assert _count(db_session, tenant) == 1


def test_professional_gets_two_and_growth_five(db_session, pg_client, fake_sidecar, queue):
    pro = make_tenant(db_session, "professional")
    _login(db_session, pro)
    assert pg_client.post(URL, json=consent_body(PHONE_A)).status_code == 201
    assert pg_client.post(URL, json=consent_body(PHONE_B)).status_code == 201
    third = pg_client.post(URL, json=consent_body(PHONE_C))
    assert third.status_code == 409 and third.json()["code"] == "connection_limit"


def test_enterprise_has_no_plan_limit(db_session, pg_client, fake_sidecar, queue):
    tenant = make_tenant(db_session, "enterprise")
    _login(db_session, tenant)
    first = pg_client.post(URL, json=consent_body(PHONE_A))
    assert first.status_code == 201, first.text
    assert pg_client.post(URL, json=consent_body(PHONE_B)).status_code == 201


def test_a_tenant_nobody_granted_can_connect_on_the_plan_alone(
    db_session, pg_client, fake_sidecar, queue
):
    tenant = make_tenant(db_session, "growth")
    caps = caps_of(db_session, tenant)
    assert caps.can_use_whatsapp is True and not caps.override_flags  # copied from the plan
    _login(db_session, tenant)
    r = pg_client.post(URL, json=consent_body(PHONE_A))
    assert r.status_code == 201, r.text


def test_the_plan_alone_still_needs_the_global_kill_switch(
    db_session, pg_client, fake_sidecar, queue, wa_settings, monkeypatch
):
    monkeypatch.setattr(wa_settings, "WHATSAPP_ENABLED", False)
    tenant = make_tenant(db_session, "growth")
    _login(db_session, tenant)
    r = pg_client.post(URL, json=consent_body(PHONE_A))
    assert r.status_code == 403 and r.json()["code"] == "whatsapp_not_enabled"
    assert _count(db_session, tenant) == 0


def test_an_admin_override_off_closes_whatsapp_on_a_plan_that_includes_it(
    db_session, pg_client, fake_sidecar, queue
):
    tenant = make_tenant(db_session, "growth", whatsapp_override=False)
    _login(db_session, tenant)
    r = pg_client.post(URL, json=consent_body(PHONE_A))
    assert r.status_code == 403 and r.json()["code"] == "whatsapp_not_enabled"


def test_an_admin_override_on_opens_whatsapp_when_the_plan_row_says_no(
    db_session, pg_client, fake_sidecar, queue
):
    from app.db.models.subscription_plan import SubscriptionPlan

    db_session.query(SubscriptionPlan).filter_by(tier="lite").update({"can_use_whatsapp": False})
    plain = make_tenant(db_session, "lite")
    granted = make_tenant(db_session, "lite", whatsapp_override=True)
    _login(db_session, plain)
    assert pg_client.post(URL, json=consent_body(PHONE_A)).status_code == 403
    _login(db_session, granted)
    assert pg_client.post(URL, json=consent_body(PHONE_B)).status_code == 201


# --- inbound/outbound gate: the same decision as the create path --------------------------------


def test_inbound_gate_is_open_for_a_plan_only_tenant(db_session, wa_settings):
    from app.services.whatsapp_inbound import _tenant_may_use

    assert _tenant_may_use(db_session, make_tenant(db_session, "lite").id) is True


def test_inbound_gate_is_closed_by_an_admin_override_off(db_session, wa_settings):
    from app.services.whatsapp_inbound import _tenant_may_use

    tenant = make_tenant(db_session, "professional", whatsapp_override=False)
    assert _tenant_may_use(db_session, tenant.id) is False


def test_inbound_gate_is_closed_when_the_kill_switch_is_off(db_session, wa_settings, monkeypatch):
    from app.services.whatsapp_inbound import _tenant_may_use

    tenant = make_tenant(db_session, "lite")
    monkeypatch.setattr(wa_settings, "WHATSAPP_ENABLED", False)
    assert _tenant_may_use(db_session, tenant.id) is False


def test_inbound_gate_ignores_a_lapsed_account_like_the_create_path(db_session, wa_settings):
    """Lapsed accounts keep today's inbound behaviour; the 402 path deals with them."""
    from app.services.whatsapp_inbound import _tenant_may_use

    tenant = make_tenant(db_session, "professional")
    tenant.subscription_status = "canceled"
    db_session.commit()
    assert _tenant_may_use(db_session, tenant.id) is True


def test_inbound_gate_is_closed_for_an_unknown_tenant(db_session, wa_settings):
    from uuid import uuid4

    from app.services.whatsapp_inbound import _tenant_may_use

    assert _tenant_may_use(db_session, uuid4()) is False


def test_inbound_gate_follows_the_plan_row_not_a_stale_stored_copy(db_session, wa_settings):
    from app.services.whatsapp_inbound import _tenant_may_use

    tenant = make_tenant(db_session, "lite")
    caps_of(db_session, tenant).can_use_whatsapp = False  # drifted copy, no override entry
    db_session.commit()
    assert _tenant_may_use(db_session, tenant.id) is True


# --- platform admin edit, end to end ------------------------------------------------------------


def test_platform_admin_can_switch_whatsapp_off_and_on_for_one_tenant(
    db_session, pg_client, super_admin_token
):
    tenant = make_tenant(db_session, "professional")
    _, headers = make_user(db_session, tenant)
    admin = {"Authorization": f"Bearer {super_admin_token}"}
    put = f"/api/v1/platform/tenants/{tenant.id}/capabilities"
    entitlements = "/api/v1/tenant/entitlements"

    assert pg_client.get(entitlements, headers=headers).json()["features"]["whatsapp"]["enabled"]

    r = pg_client.put(put, headers=admin, json={"can_use_whatsapp": False})
    assert r.status_code == 200, r.text
    assert r.json()["can_use_whatsapp"] is False
    whatsapp = pg_client.get(entitlements, headers=headers).json()["features"]["whatsapp"]
    assert whatsapp["enabled"] is False
    assert whatsapp["limit"] == 2  # the plan's numbers are untouched

    r = pg_client.put(put, headers=admin, json={"can_use_whatsapp": True})
    assert r.status_code == 200, r.text
    assert pg_client.get(entitlements, headers=headers).json()["features"]["whatsapp"]["enabled"]


def test_a_hand_set_number_overrides_the_plan(db_session, pg_client, fake_sidecar, queue):
    tenant = make_tenant(db_session, "lite")
    caps = caps_of(db_session, tenant)
    caps.max_whatsapp_numbers = 2
    caps.override_flags = {"max_whatsapp_numbers": True}
    db_session.commit()
    _login(db_session, tenant)
    assert pg_client.post(URL, json=consent_body(PHONE_A)).status_code == 201
    assert pg_client.post(URL, json=consent_body(PHONE_B)).status_code == 201
