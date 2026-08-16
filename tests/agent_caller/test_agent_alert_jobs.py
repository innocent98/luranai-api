"""Behaviour tests for the escalation ARQ jobs."""

import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest

from app.db.models.agent_device import AgentDevice, DevicePlatform
from app.db.models.conversation import (
    ConversationSession,
    SessionStatus,
    Ticket,
    TicketPriority,
    TicketStatus,
)
from app.db.models.notification_log import NotificationLog, NotifEvent, NotifStatus
from app.db.models.user import AvailabilityStatus, User, UserRole
from app.services.fcm_client import FCMResult
from app.utils.datetime import utcnow_naive
from app.workers import agent_alerts


@pytest.fixture
def patched_session(db, monkeypatch):
    """Make the jobs use the test session instead of opening their own."""
    monkeypatch.setattr(agent_alerts, "SessionLocal", lambda: db)
    monkeypatch.setattr(db, "close", lambda: None)
    return db


def _user(db, tenant_id, email, role, availability=AvailabilityStatus.AVAILABLE, token=None):
    user = User(
        email=email,
        hashed_password="x",
        full_name=email.split("@")[0].title(),
        role=role,
        tenant_id=tenant_id,
        is_active=True,
        availability_status=availability,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    if token:
        db.add(
            AgentDevice(
                user_id=user.id,
                tenant_id=tenant_id,
                device_token=token,
                platform=DevicePlatform.ANDROID,
            )
        )
        db.commit()
    return user


def _escalation(db, tenant_id):
    session = ConversationSession(
        tenant_id=tenant_id,
        user_identifier=f"visitor:{uuid.uuid4().hex[:8]}",
        status=SessionStatus.ESCALATED,
        escalation_reason="Refund",
        escalated_at=utcnow_naive() - timedelta(minutes=2),
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    ticket = Ticket(
        tenant_id=tenant_id,
        session_id=session.id,
        ticket_number=f"TKT-{uuid.uuid4().hex[:8]}",
        summary="Refund",
        priority=TicketPriority.HIGH,
        status=TicketStatus.OPEN,
    )
    db.add(ticket)
    db.commit()
    db.refresh(ticket)
    return ticket, session


@pytest.mark.asyncio
async def test_alert_escalation_rings_available_agents_only(patched_session):
    db = patched_session
    tenant_id = uuid.uuid4()
    _user(db, tenant_id, "up@x.com", UserRole.SUPPORT_AGENT, token="tok-up")
    _user(db, tenant_id, "away@x.com", UserRole.SUPPORT_AGENT, AvailabilityStatus.AWAY, "tok-away")
    ticket, _ = _escalation(db, tenant_id)

    sent = AsyncMock(side_effect=lambda tok, data, **kw: FCMResult(token=tok, ok=True))
    with patch.object(agent_alerts.fcm_client, "send", sent):
        await agent_alerts.alert_escalation({}, str(ticket.id))

    rung = {call.args[0] for call in sent.await_args_list}
    assert rung == {"tok-up"}


@pytest.mark.asyncio
async def test_alert_escalation_is_a_noop_when_disabled(patched_session, monkeypatch):
    db = patched_session
    tenant_id = uuid.uuid4()
    _user(db, tenant_id, "up@x.com", UserRole.SUPPORT_AGENT, token="tok-up")
    ticket, _ = _escalation(db, tenant_id)
    monkeypatch.setattr(agent_alerts.settings, "AGENT_ALERT_ENABLED", False)

    sent = AsyncMock()
    with patch.object(agent_alerts.fcm_client, "send", sent):
        await agent_alerts.alert_escalation({}, str(ticket.id))

    sent.assert_not_awaited()


@pytest.mark.asyncio
async def test_rerunning_the_job_does_not_double_push(patched_session):
    db = patched_session
    tenant_id = uuid.uuid4()
    _user(db, tenant_id, "up@x.com", UserRole.SUPPORT_AGENT, token="tok-up")
    ticket, _ = _escalation(db, tenant_id)

    sent = AsyncMock(side_effect=lambda tok, data, **kw: FCMResult(token=tok, ok=True))
    with patch.object(agent_alerts.fcm_client, "send", sent):
        await agent_alerts.alert_escalation({}, str(ticket.id))
        await agent_alerts.alert_escalation({}, str(ticket.id))

    assert sent.await_count == 1


@pytest.mark.asyncio
async def test_unregistered_token_is_pruned(patched_session):
    db = patched_session
    tenant_id = uuid.uuid4()
    user = _user(db, tenant_id, "up@x.com", UserRole.SUPPORT_AGENT, token="tok-dead")
    ticket, _ = _escalation(db, tenant_id)

    sent = AsyncMock(
        side_effect=lambda tok, data, **kw: FCMResult(token=tok, ok=False, unregistered=True)
    )
    with patch.object(agent_alerts.fcm_client, "send", sent):
        await agent_alerts.alert_escalation({}, str(ticket.id))

    assert db.query(AgentDevice).filter(AgentDevice.user_id == user.id).count() == 0


@pytest.mark.asyncio
async def test_fallback_noops_when_already_claimed(patched_session):
    db = patched_session
    tenant_id = uuid.uuid4()
    admin = _user(db, tenant_id, "boss@x.com", UserRole.TENANT_ADMIN, token="tok-boss")
    ticket, _ = _escalation(db, tenant_id)
    ticket.assigned_to = admin.id
    ticket.status = TicketStatus.IN_PROGRESS
    db.commit()

    sent = AsyncMock()
    with patch.object(agent_alerts.fcm_client, "send", sent):
        await agent_alerts.alert_escalation_fallback({}, str(ticket.id))

    sent.assert_not_awaited()


@pytest.mark.asyncio
async def test_fallback_rings_supervisors_and_misses_tier_one(patched_session):
    db = patched_session
    tenant_id = uuid.uuid4()
    _user(db, tenant_id, "up@x.com", UserRole.SUPPORT_AGENT, token="tok-up")
    _user(db, tenant_id, "boss@x.com", UserRole.TENANT_ADMIN, token="tok-boss")
    ticket, _ = _escalation(db, tenant_id)

    sent = AsyncMock(side_effect=lambda tok, data, **kw: FCMResult(token=tok, ok=True))
    with patch.object(agent_alerts.fcm_client, "send", sent):
        await agent_alerts.alert_escalation({}, str(ticket.id))
        await agent_alerts.alert_escalation_fallback({}, str(ticket.id))

    by_token = {call.args[0]: call.args[1] for call in sent.await_args_list}
    assert by_token["tok-boss"]["type"] == "escalation_alert"
    assert by_token["tok-up"]["type"] == "missed_escalation"
    assert (
        db.query(NotificationLog)
        .filter(NotificationLog.event == NotifEvent.ESCALATION_FALLBACK)
        .count()
        == 1
    )


@pytest.mark.asyncio
async def test_fallback_marks_the_ticket_missed_when_nobody_answers(patched_session):
    db = patched_session
    tenant_id = uuid.uuid4()
    _user(db, tenant_id, "up@x.com", UserRole.SUPPORT_AGENT, token="tok-up")
    ticket, _ = _escalation(db, tenant_id)

    sent = AsyncMock(side_effect=lambda tok, data, **kw: FCMResult(token=tok, ok=True))
    with patch.object(agent_alerts.fcm_client, "send", sent):
        await agent_alerts.alert_escalation({}, str(ticket.id))
        await agent_alerts.alert_escalation_fallback({}, str(ticket.id))

    db.refresh(ticket)
    assert ticket.status == TicketStatus.MISSED


@pytest.mark.asyncio
async def test_clear_skips_the_winner(patched_session):
    db = patched_session
    tenant_id = uuid.uuid4()
    winner = _user(db, tenant_id, "win@x.com", UserRole.SUPPORT_AGENT, token="tok-win")
    _user(db, tenant_id, "lose@x.com", UserRole.SUPPORT_AGENT, token="tok-lose")
    ticket, _ = _escalation(db, tenant_id)

    sent = AsyncMock(side_effect=lambda tok, data, **kw: FCMResult(token=tok, ok=True))
    with patch.object(agent_alerts.fcm_client, "send", sent):
        await agent_alerts.alert_escalation({}, str(ticket.id))
        sent.reset_mock()
        await agent_alerts.clear_agent_alert({}, str(ticket.id), str(winner.id))

    rung = {call.args[0] for call in sent.await_args_list}
    assert rung == {"tok-lose"}
    assert all(call.args[1]["type"] == "clear_alert" for call in sent.await_args_list)


@pytest.mark.asyncio
async def test_failed_send_marks_the_log_failed(patched_session):
    db = patched_session
    tenant_id = uuid.uuid4()
    _user(db, tenant_id, "up@x.com", UserRole.SUPPORT_AGENT, token="tok-up")
    ticket, _ = _escalation(db, tenant_id)

    sent = AsyncMock(
        side_effect=lambda tok, data, **kw: FCMResult(token=tok, ok=False, error="503")
    )
    with patch.object(agent_alerts.fcm_client, "send", sent):
        await agent_alerts.alert_escalation({}, str(ticket.id))

    log = db.query(NotificationLog).one()
    assert log.status == NotifStatus.FAILED
    assert "503" in (log.error or "")
