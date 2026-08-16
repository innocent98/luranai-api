from app.core.config import settings
from app.db.models.tenant import TenantCapabilities


def test_voice_settings_exist_with_safe_defaults():
    assert settings.VOICE_SESSION_TOKEN_TTL_SECONDS == 600
    assert settings.VOICE_MAX_CONCURRENT_SESSIONS == 20
    # Secrets default to None so a missing .env fails closed rather than booting insecure.
    assert hasattr(settings, "ELEVENLABS_API_KEY")
    assert hasattr(settings, "VOICE_SHARED_SECRET")


def test_capabilities_default_voice_off():
    cap = TenantCapabilities()
    assert cap.can_use_voice is False or cap.can_use_voice is None
    assert cap.max_voice_minutes_per_month is None
