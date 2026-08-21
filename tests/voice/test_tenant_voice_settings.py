import pytest
from uuid import uuid4

from app.core.security import create_access_token
from app.db.models.tenant import TenantCapabilities
from app.db.models.user import User, UserRole
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
    would make the response shape conditional and harder to consume.
    """
    _, token = voice_admin(can_use_voice=False)

    resp = pg_client.get(
        "/api/v1/tenant/voice/settings", headers={"Authorization": f"Bearer {token}"}
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["granted"] is False
    assert "enabled" in body
    assert "minutes_used" in body
    assert "available_voices" in body


def test_get_requires_authentication(pg_client):
    """Would fail if the route were registered without the tenant-admin dependency."""
    resp = pg_client.get("/api/v1/tenant/voice/settings")
    assert resp.status_code in (401, 403)
