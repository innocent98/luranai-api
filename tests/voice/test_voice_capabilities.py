from app.core.config import Settings
from app.db.models.tenant import TenantCapabilities
from tests.conftest import _bare_voice_tenant as _tenant


def test_voice_settings_exist_with_safe_defaults():
    """Assert the DECLARED defaults, not the live singleton.

    Reading `settings.X` here would read whatever `.env` happens to hold, so this
    test passed only while nobody had configured a real key -- and then failed the
    moment one was, printing the real secret into pytest's assertion output. The
    property we actually care about is that the *declared* default is None, so a
    missing .env fails closed rather than booting insecure. Checking
    `model_fields` tests exactly that, is independent of the environment, and
    cannot leak a configured value.
    """
    fields = Settings.model_fields
    assert fields["VOICE_SESSION_TOKEN_TTL_SECONDS"].default == 600
    assert fields["VOICE_MAX_CONCURRENT_SESSIONS"].default == 20
    assert fields["ELEVENLABS_API_KEY"].default is None
    assert fields["VOICE_SHARED_SECRET"].default is None
    assert fields["ELEVENLABS_AGENT_ID"].default is None


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
