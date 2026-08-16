from app.core.config import settings
from app.db.models.tenant import TenantCapabilities
from tests.conftest import _bare_voice_tenant as _tenant


def test_voice_settings_exist_with_safe_defaults():
    assert settings.VOICE_SESSION_TOKEN_TTL_SECONDS == 600
    assert settings.VOICE_MAX_CONCURRENT_SESSIONS == 20
    # Secrets default to None so a missing .env fails closed rather than booting insecure.
    assert settings.ELEVENLABS_API_KEY is None
    assert settings.VOICE_SHARED_SECRET is None


def test_capabilities_default_voice_off(db_session):
    """Persist a real row and read the defaults back — exercises the DB
    server_default, not just the in-memory Python default."""
    tenant = _tenant(db_session)
    cap = TenantCapabilities(tenant_id=tenant.id)
    db_session.add(cap)
    db_session.flush()
    db_session.refresh(cap)

    assert cap.can_use_voice is False
    assert cap.max_voice_minutes_per_month is None
