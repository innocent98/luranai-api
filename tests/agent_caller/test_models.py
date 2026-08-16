"""Schema-level tests for the Agent Caller data model."""

import uuid

import pytest

from app.db.models.agent_device import AgentDevice, DevicePlatform
from app.db.models.conversation import TicketStatus
from app.db.models.notification_log import NotifChannel, NotifEvent
from app.db.models.user import AvailabilityStatus, User, UserRole


def _user(db, tenant_id, email="agent@example.com", role=UserRole.SUPPORT_AGENT):
    user = User(
        email=email,
        hashed_password="x",
        full_name="Agent One",
        role=role,
        tenant_id=tenant_id,
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def test_new_enum_values_exist():
    assert TicketStatus.MISSED.value == "missed"
    assert NotifChannel.PUSH.value == "push"
    assert NotifEvent.ESCALATION_ALERT.value == "escalation_alert"
    assert NotifEvent.ESCALATION_FALLBACK.value == "escalation_fallback"
    assert NotifEvent.ESCALATION_MISSED.value == "escalation_missed"


def test_user_defaults_to_available(db):
    tenant_id = uuid.uuid4()
    user = _user(db, tenant_id)
    assert user.availability_status == AvailabilityStatus.AVAILABLE


def test_agent_device_round_trips(db):
    tenant_id = uuid.uuid4()
    user = _user(db, tenant_id)
    device = AgentDevice(
        user_id=user.id,
        tenant_id=tenant_id,
        device_token="tok-abc",
        platform=DevicePlatform.ANDROID,
    )
    db.add(device)
    db.commit()
    db.refresh(device)
    assert device.platform is DevicePlatform.ANDROID
    assert device.device_token == "tok-abc"


def test_duplicate_user_token_is_rejected(db):
    from sqlalchemy.exc import IntegrityError

    tenant_id = uuid.uuid4()
    user = _user(db, tenant_id)
    db.add(
        AgentDevice(
            user_id=user.id,
            tenant_id=tenant_id,
            device_token="dupe",
            platform=DevicePlatform.IOS,
        )
    )
    db.commit()
    db.add(
        AgentDevice(
            user_id=user.id,
            tenant_id=tenant_id,
            device_token="dupe",
            platform=DevicePlatform.IOS,
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_ticket_acknowledged_at_column_exists():
    from app.db.models.conversation import Ticket

    assert "acknowledged_at" in Ticket.__table__.columns
