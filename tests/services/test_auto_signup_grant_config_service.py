from datetime import UTC, datetime, timedelta

import pytest

from app.services.auto_signup_grant_config_service import (
    AutoSignupGrantConfigService,
    AutoSignupConfigValidationError,
)


def test_get_returns_singleton_default(db_session):
    service = AutoSignupGrantConfigService(db_session)
    config = service.get()
    assert config.enabled is False
    assert config.campaign_code is None


def test_update_enables_with_full_config(db_session, super_admin_user):
    service = AutoSignupGrantConfigService(db_session)
    valid_until = datetime.now(UTC) + timedelta(days=90)
    updated = service.update(
        enabled=True,
        campaign_code="LAUNCH_2026",
        campaign_name="Public Launch 2026",
        granted_tier="professional",
        duration_days=90,
        valid_until=valid_until,
        admin_id=super_admin_user.id,
        admin_name=super_admin_user.email,
    )
    assert updated.enabled is True
    assert updated.campaign_code == "LAUNCH_2026"
    assert updated.granted_tier == "professional"
    assert updated.duration_days == 90
    assert updated.updated_by_admin_id == super_admin_user.id


def test_update_rejects_enabled_without_required_fields(db_session, super_admin_user):
    service = AutoSignupGrantConfigService(db_session)
    with pytest.raises(AutoSignupConfigValidationError):
        service.update(
            enabled=True,
            campaign_code=None,
            campaign_name=None,
            granted_tier=None,
            duration_days=None,
            valid_until=None,
            admin_id=super_admin_user.id,
            admin_name=super_admin_user.email,
        )


def test_update_disabling_clears_optional_fields_preserved(db_session, super_admin_user):
    service = AutoSignupGrantConfigService(db_session)
    service.update(
        enabled=True,
        campaign_code="LAUNCH_2026",
        campaign_name="Public Launch 2026",
        granted_tier="professional",
        duration_days=90,
        valid_until=None,
        admin_id=super_admin_user.id,
        admin_name=super_admin_user.email,
    )
    disabled = service.update(
        enabled=False,
        campaign_code="LAUNCH_2026",
        campaign_name="Public Launch 2026",
        granted_tier="professional",
        duration_days=90,
        valid_until=None,
        admin_id=super_admin_user.id,
        admin_name=super_admin_user.email,
    )
    assert disabled.enabled is False
    assert disabled.campaign_code == "LAUNCH_2026"


def test_is_active_handles_valid_until(db_session, super_admin_user):
    service = AutoSignupGrantConfigService(db_session)
    past = datetime.now(UTC) - timedelta(days=1)
    service.update(
        enabled=True,
        campaign_code="LAUNCH_2026",
        campaign_name="Public Launch 2026",
        granted_tier="professional",
        duration_days=90,
        valid_until=past,
        admin_id=super_admin_user.id,
        admin_name=super_admin_user.email,
    )
    assert service.is_active() is False


def test_is_active_no_valid_until_means_indefinite(db_session, super_admin_user):
    service = AutoSignupGrantConfigService(db_session)
    service.update(
        enabled=True,
        campaign_code="LAUNCH_2026",
        campaign_name="Public Launch 2026",
        granted_tier="professional",
        duration_days=90,
        valid_until=None,
        admin_id=super_admin_user.id,
        admin_name=super_admin_user.email,
    )
    assert service.is_active() is True
