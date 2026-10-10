"""Behaviour tests for the escalation ARQ jobs."""

import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import update

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
from app.services.agent_alert_service import AgentAlertService
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
@pytest.mark.parametrize(
    "job, args",
    [
        (agent_alerts.alert_escalation, ("dummy-ticket",)),
        (agent_alerts.alert_escalation_fallback, ("dummy-ticket",)),
        (agent_alerts.clear_agent_alert, ("dummy-ticket", "dummy-winner")),
    ],
    ids=["alert_escalation", "alert_escalation_fallback", "clear_agent_alert"],
)
async def test_disabled_flag_short_circuits_every_job(patched_session, monkeypatch, job, args):
    """AGENT_ALERT_ENABLED=False is the production kill switch — it must stop all three jobs."""
    monkeypatch.setattr(agent_alerts.settings, "AGENT_ALERT_ENABLED", False)

    sent = AsyncMock()
    with patch.object(agent_alerts.fcm_client, "send", sent):
        await job({}, *args)

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

    # The silent/loud split is the entire point of these two tiers — assert it
    # directly on `notification`, not just on payload `type`, so flipping the
    # `visible` flag on either call site fails the suite.
    boss_calls = [call for call in sent.await_args_list if call.args[0] == "tok-boss"]
    assert boss_calls
    assert all(
        call.kwargs.get("notification") is not None for call in boss_calls
    ), "the supervisor page must be a real, visible ring"

    missed_calls = [
        call
        for call in sent.await_args_list
        if call.args[0] == "tok-up" and call.args[1]["type"] == "missed_escalation"
    ]
    assert len(missed_calls) == 1
    assert (
        missed_calls[0].kwargs.get("notification") is None
    ), "the missed notice must be silent — a loud second ring is what this must prevent"

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
async def test_claim_landing_during_fallback_skips_supervisor_ring_and_stays_unmissed(
    patched_session,
):
    """F2/F3 regression: a claim landing between the fallback's job-start check
    and the actual supervisor ring must skip that ring entirely, and must not
    flip the ticket to MISSED — it's being actively handled, not missed."""
    db = patched_session
    tenant_id = uuid.uuid4()
    agent = _user(db, tenant_id, "up@x.com", UserRole.SUPPORT_AGENT, token="tok-up")
    _user(db, tenant_id, "boss@x.com", UserRole.TENANT_ADMIN, token="tok-boss")
    ticket, _ = _escalation(db, tenant_id)

    real_supervisor_recipients = AgentAlertService.supervisor_recipients

    def _claim_then_list_supervisors(self, tenant_id_arg):
        # Simulate the race this fix targets: an agent claims the ticket after
        # the job-start check already passed, but before the fallback pushes
        # anyone — the gap the pre-push re-read is meant to close.
        db.execute(
            update(Ticket)
            .where(Ticket.id == ticket.id, Ticket.assigned_to.is_(None))
            .values(assigned_to=agent.id, status=TicketStatus.IN_PROGRESS)
        )
        db.commit()
        return real_supervisor_recipients(self, tenant_id_arg)

    sent = AsyncMock(side_effect=lambda tok, data, **kw: FCMResult(token=tok, ok=True))
    with (
        patch.object(agent_alerts.fcm_client, "send", sent),
        patch.object(AgentAlertService, "supervisor_recipients", _claim_then_list_supervisors),
    ):
        await agent_alerts.alert_escalation({}, str(ticket.id))
        sent.reset_mock()
        await agent_alerts.alert_escalation_fallback({}, str(ticket.id))

    sent.assert_not_awaited()
    db.refresh(ticket)
    assert ticket.assigned_to == agent.id
    assert ticket.status == TicketStatus.IN_PROGRESS


@pytest.mark.asyncio
async def test_supervisor_who_loses_the_race_receives_a_late_clear(patched_session):
    """F2 regression: a claim landing mid-fallback-ring — after the pre-push
    re-check passes but while supervisors are actively being pushed — must
    not leave a supervisor's phone ringing forever on an already-handled
    ticket. The clear enqueued here must be able to fire even though the
    claim endpoint's own clear (a different job-id suffix) may already have."""
    db = patched_session
    tenant_id = uuid.uuid4()
    agent = _user(db, tenant_id, "up@x.com", UserRole.SUPPORT_AGENT, token="tok-up")
    # AWAY: supervisor_recipients ignores availability (a supervisor rings
    # regardless), but this must stay out of the tier-1 broadcast, or the
    # side effect below fires during `alert_escalation` instead of the
    # fallback's supervisor push that this test targets.
    _user(
        db,
        tenant_id,
        "boss@x.com",
        UserRole.TENANT_ADMIN,
        AvailabilityStatus.AWAY,
        token="tok-boss",
    )
    ticket, _ = _escalation(db, tenant_id)

    def _send(tok, data, **kw):
        if tok == "tok-boss":
            # The claim lands while the supervisor's device is being pushed —
            # after this fallback run already confirmed the ticket was open.
            db.execute(
                update(Ticket)
                .where(Ticket.id == ticket.id, Ticket.assigned_to.is_(None))
                .values(assigned_to=agent.id, status=TicketStatus.IN_PROGRESS)
            )
            db.commit()
        return FCMResult(token=tok, ok=True)

    sent = AsyncMock(side_effect=_send)
    clear_enqueue = AsyncMock()
    with (
        patch.object(agent_alerts.fcm_client, "send", sent),
        patch.object(agent_alerts, "enqueue_clear_alert_async", clear_enqueue),
    ):
        await agent_alerts.alert_escalation({}, str(ticket.id))
        sent.reset_mock()
        await agent_alerts.alert_escalation_fallback({}, str(ticket.id))

    clear_enqueue.assert_awaited_once_with(ticket.id, agent.id, source="fallback")
    db.refresh(ticket)
    assert ticket.status == TicketStatus.IN_PROGRESS, "must not be overwritten back to MISSED"


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
    assert all(
        call.kwargs.get("notification") is None for call in sent.await_args_list
    ), "clear_agent_alert must be silent — a visible clear would ring the loser's phone again"


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


@pytest.mark.asyncio
async def test_fallback_posts_missed_notice_once(patched_session):
    from app.db.models.conversation import Message, MessageRole
    from app.services.first_responder import MISSED_NOTICE

    db = patched_session
    tenant_id = uuid.uuid4()
    _user(db, tenant_id, "up@x.com", UserRole.SUPPORT_AGENT, token="tok-up")
    ticket, session = _escalation(db, tenant_id)

    fan = AsyncMock()
    sent = AsyncMock(side_effect=lambda tok, data, **kw: FCMResult(token=tok, ok=True))
    with (
        patch.object(agent_alerts.fcm_client, "send", sent),
        patch.object(agent_alerts.connection_manager, "fanout_event", fan),
    ):
        await agent_alerts.alert_escalation_fallback({}, str(ticket.id))
        await agent_alerts.alert_escalation_fallback({}, str(ticket.id))  # retry

    # content is encrypted at rest (non-deterministic), so compare in Python.
    notices = [
        m
        for m in db.query(Message).filter(Message.session_id == session.id).all()
        if m.content == MISSED_NOTICE
    ]
    assert len(notices) == 1
    assert notices[0].role == MessageRole.ASSISTANT
    frames = [c.kwargs["message"] for c in fan.await_args_list]
    assert [f["type"] for f in frames] == ["ticket_missed"]
    assert fan.await_args_list[0].kwargs["to_customer"] is True


@pytest.mark.asyncio
async def test_fallback_no_notice_when_claimed(patched_session):
    from app.db.models.conversation import Message
    from app.services.first_responder import MISSED_NOTICE

    db = patched_session
    tenant_id = uuid.uuid4()
    admin = _user(db, tenant_id, "boss@x.com", UserRole.TENANT_ADMIN, token="tok-boss")
    ticket, session = _escalation(db, tenant_id)
    ticket.assigned_to = admin.id
    ticket.status = TicketStatus.IN_PROGRESS
    db.commit()
    await agent_alerts.alert_escalation_fallback({}, str(ticket.id))
    msgs = db.query(Message).filter(Message.session_id == session.id).all()
    assert [m for m in msgs if m.content == MISSED_NOTICE] == []
