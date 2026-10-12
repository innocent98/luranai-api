"""screen_recording_allowed = capability AND tenant preference; fails closed."""

from unittest.mock import patch

from app.db.models.tenant import Tenant, TenantCapabilities
from app.schemas.tenant import TenantCapabilitiesUpdate
from app.services.screen_recording.gating import screen_recording_allowed
from app.services.tenant_service import TenantService
from tests.api.v1 import test_chat_image_send as img


def _tenant(db, *, cap: bool, pref: bool):
    _, tid = img._session(db)  # seeds TenantCapabilities
    db.query(TenantCapabilities).filter_by(tenant_id=tid).update({"can_use_screen_recording": cap})
    db.query(Tenant).filter_by(id=tid).update({"widget_allow_screen_recording": pref})
    db.commit()
    return db.get(Tenant, tid)


def test_both_on(db):
    assert screen_recording_allowed(db, _tenant(db, cap=True, pref=True)) is True


def test_capability_off(db):
    assert screen_recording_allowed(db, _tenant(db, cap=False, pref=True)) is False


def test_preference_off(db):
    assert screen_recording_allowed(db, _tenant(db, cap=True, pref=False)) is False


def test_missing_capabilities_row_fails_closed(db):
    t = _tenant(db, cap=True, pref=True)
    db.query(TenantCapabilities).filter_by(tenant_id=t.id).delete()
    db.commit()
    assert screen_recording_allowed(db, t) is False


def test_none_tenant_and_exception_fail_closed(db):
    assert screen_recording_allowed(db, None) is False
    t = _tenant(db, cap=True, pref=True)
    with patch.object(db, "execute", side_effect=RuntimeError("boom")):
        assert screen_recording_allowed(db, t) is False


def test_public_widget_config_exposes_flag(client, db):
    from tests.api.v1.test_widget_public_config_first_responder import _get_config

    on_id = _tenant(db, cap=True, pref=True).id
    off_id = _tenant(db, cap=False, pref=True).id
    r_on, r_off = _get_config(client, on_id), _get_config(client, off_id)
    assert r_on.status_code == 200, r_on.text
    assert r_on.json()["allow_screen_recording"] is True
    assert r_off.json()["allow_screen_recording"] is False


def test_platform_capability_update_persists_and_marks_override(db):
    t = _tenant(db, cap=True, pref=True)
    resp = TenantService(db).update_capabilities(
        t.id, TenantCapabilitiesUpdate(can_use_screen_recording=False)
    )
    assert resp.can_use_screen_recording is False
    caps = db.query(TenantCapabilities).filter_by(tenant_id=t.id).one()
    assert caps.can_use_screen_recording is False
    assert caps.override_flags["can_use_screen_recording"] is True
    assert screen_recording_allowed(db, db.get(Tenant, t.id)) is False
