import pytest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from app.core.security import create_access_token
from app.db.models.tenant import TenantAIConfig, TenantCapabilities
from app.db.models.user import User, UserRole
from app.services.voice.metering import VoiceMeter
from app.services.voice.voices import CURATED_VOICES
from tests.conftest import _bare_voice_tenant as _tenant


@pytest.fixture
def voice_admin(db_session):
    """A tenant admin plus their tenant — the pair the endpoints need.

    `regular_user_token` returns only a token, so tests using it cannot set up
    or assert on the tenant. This returns both.
    """

    def _make(*, can_use_voice: bool, max_voice_minutes_per_month: int | None = None):
        tenant = _tenant(db_session)
        db_session.add(
            TenantCapabilities(
                tenant_id=tenant.id,
                can_use_voice=can_use_voice,
                max_voice_minutes_per_month=max_voice_minutes_per_month,
            )
        )
        user = User(
            email=f"admin_{uuid4().hex[:8]}@example.com",
            hashed_password="$2b$12$placeholder_hash_not_real",
            full_name="Voice Admin",
            role=UserRole.TENANT_ADMIN,
            tenant_id=tenant.id,
            is_active=True,
        )
        db_session.add(user)
        db_session.commit()
        db_session.refresh(user)
        token = create_access_token(
            subject=str(user.id),
            additional_claims={"role": user.role.value, "tenant_id": str(user.tenant_id)},
        )
        return tenant, token

    return _make


def test_get_returns_grant_preference_and_usage(
    pg_client, db_session, voice_admin, voice_usage_setter
):
    """One call gives the page everything it needs.

    Would fail if `granted` were sourced from the preference (or vice versa) —
    the two must not be conflated, since that conflation is exactly what would
    let a tenant appear to self-grant.
    """
    tenant, token = voice_admin(can_use_voice=True, max_voice_minutes_per_month=500)
    voice_usage_setter(tenant.id, minutes=142)

    resp = pg_client.get(
        "/api/v1/tenant/voice/settings", headers={"Authorization": f"Bearer {token}"}
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["granted"] is True
    assert body["enabled"] is True
    assert body["voice_id"] is None
    assert body["minutes_limit"] == 500
    assert body["minutes_used"] == pytest.approx(142, abs=1)
    assert len(body["available_voices"]) >= 1


def test_get_returns_every_field_even_when_not_granted(pg_client, voice_admin):
    """The response shape is unconditional — the page decides what to render.

    Would fail if the endpoint omitted fields when `granted` is false, which
    would make the response shape conditional and harder to consume. Asserts
    on actual values rather than key presence: presence alone is guaranteed
    by the response model (FastAPI would 500 on serialization otherwise, and
    the status_code assertion already catches that), so a presence-only check
    can never fail and proves nothing.
    """
    _, token = voice_admin(can_use_voice=False)

    resp = pg_client.get(
        "/api/v1/tenant/voice/settings", headers={"Authorization": f"Bearer {token}"}
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["granted"] is False
    assert body["enabled"] is True
    assert body["minutes_used"] == pytest.approx(0, abs=1)
    assert len(body["available_voices"]) >= 1


def test_get_reflects_tenant_ai_config_preference(pg_client, db_session, voice_admin):
    """The preference half of the contract: `enabled`/`voice_id` must come
    from the tenant's own `TenantAIConfig` row, not just its `config is None`
    defaults.

    Would fail if the endpoint hardcoded `enabled=True, voice_id=None` and
    never read `TenantAIConfig` at all — `voice_admin` alone never creates
    that row, so `test_get_returns_grant_preference_and_usage` only ever
    exercises the default path. This test creates a config row with
    non-default values and asserts they round-trip.
    """
    tenant, token = voice_admin(can_use_voice=True)
    chosen_voice_id = CURATED_VOICES[0].id
    db_session.add(
        TenantAIConfig(tenant_id=tenant.id, voice_enabled=False, voice_id=chosen_voice_id)
    )
    db_session.commit()

    resp = pg_client.get(
        "/api/v1/tenant/voice/settings", headers={"Authorization": f"Bearer {token}"}
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is False
    assert body["voice_id"] == chosen_voice_id


def test_usage_degrades_to_null_when_the_meter_fails(pg_client, voice_admin):
    """A settings page that 500s because a cache is down is worse than one
    missing a number. Patching the meter exercises the degradation branch
    without needing Redis reachable at all -- which is why the fixture's
    seeding dependency is not a blocker here.

    Would fail if the `try/except` around `get_monthly_minutes` were widened,
    removed, or made to re-raise -- the response would 500 instead of
    degrading to `minutes_used: null`.
    """
    _, token = voice_admin(can_use_voice=True, max_voice_minutes_per_month=500)

    with patch.object(
        VoiceMeter, "get_monthly_minutes", AsyncMock(side_effect=Exception("redis down"))
    ):
        resp = pg_client.get(
            "/api/v1/tenant/voice/settings", headers={"Authorization": f"Bearer {token}"}
        )

    assert resp.status_code == 200
    assert resp.json()["minutes_used"] is None


def test_get_requires_authentication(pg_client):
    """Would fail if the route were registered without the tenant-admin dependency."""
    resp = pg_client.get("/api/v1/tenant/voice/settings")
    assert resp.status_code in (401, 403)


def test_put_updates_preference_and_voice(pg_client, db_session, voice_admin):
    """Would fail if the write silently no-opped — assert the stored row, not
    just the response body."""
    tenant, token = voice_admin(can_use_voice=True)
    voice_id = CURATED_VOICES[0].id

    resp = pg_client.put(
        "/api/v1/tenant/voice/settings",
        headers={"Authorization": f"Bearer {token}"},
        json={"enabled": False, "voice_id": voice_id},
    )

    assert resp.status_code == 200
    db_session.expire_all()
    config = db_session.query(TenantAIConfig).filter(TenantAIConfig.tenant_id == tenant.id).first()
    assert config.voice_enabled is False
    assert config.voice_id == voice_id


def test_put_cannot_grant_the_capability(pg_client, db_session, voice_admin):
    """THE security test. A tenant must never self-grant a paid capability.

    Posts `can_use_voice` in the body and asserts the grant is unchanged. The
    schema has no such field, so this is a regression guard against someone
    adding one — it would fail the moment `can_use_voice` became writable here.
    """
    tenant, token = voice_admin(can_use_voice=False)

    resp = pg_client.put(
        "/api/v1/tenant/voice/settings",
        headers={"Authorization": f"Bearer {token}"},
        json={"enabled": True, "voice_id": None, "can_use_voice": True},
    )

    assert resp.status_code == 200
    db_session.expire_all()
    caps = (
        db_session.query(TenantCapabilities)
        .filter(TenantCapabilities.tenant_id == tenant.id)
        .first()
    )
    assert caps.can_use_voice is False


def test_put_rejects_a_voice_id_we_do_not_offer(pg_client, voice_admin):
    """Reject at write time, not mint time — far from the mistake is far too late.

    Would fail if validation were missing: an arbitrary id would be stored and
    then fail on a live call."""
    _, token = voice_admin(can_use_voice=True)

    resp = pg_client.put(
        "/api/v1/tenant/voice/settings",
        headers={"Authorization": f"Bearer {token}"},
        json={"enabled": True, "voice_id": "not-a-voice-we-offer"},
    )

    assert resp.status_code == 422


def test_put_requires_authentication(pg_client):
    """Would fail if the route were registered without the tenant-admin dependency."""
    resp = pg_client.put("/api/v1/tenant/voice/settings", json={"enabled": True, "voice_id": None})
    assert resp.status_code in (401, 403)
