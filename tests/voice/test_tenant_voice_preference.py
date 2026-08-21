from app.db.models.tenant import TenantAIConfig
from tests.conftest import _bare_voice_tenant as _tenant


def test_voice_preference_defaults_to_enabled_with_no_voice_chosen(db_session):
    """Defaults matter: granting a tenant voice must make it work immediately.

    Would fail if `voice_enabled` defaulted to False (voice granted but silently
    dead) or if `voice_id` defaulted to a string (a fake id reaching the mint).
    Flushes through the session so the DB server_default is exercised, not just
    the Python-side default.
    """
    tenant = _tenant(db_session)
    config = TenantAIConfig(tenant_id=tenant.id)
    db_session.add(config)
    db_session.flush()
    db_session.refresh(config)

    assert config.voice_enabled is True
    assert config.voice_id is None
