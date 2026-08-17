"""Unit tests for AgentAlertService recipient tiers, payloads and log dedupe."""

import uuid
from datetime import timedelta

import pytest

from app.db.models.agent_device import AgentDevice, DevicePlatform
from app.db.models.conversation import (
    ConversationSession,
    SessionStatus,
    Ticket,
    TicketPriority,
    TicketStatus,
)
from app.db.models.notification_log import NotifEvent
from app.db.models.user import AvailabilityStatus, User, UserRole
from app.services.agent_alert_service import AgentAlertService
from app.utils.datetime import utcnow_naive


def _make_user(db, tenant_id, email, role, availability=AvailabilityStatus.AVAILABLE):
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
    return user


def _make_device(db, user, token="tok"):
    device = AgentDevice(
        user_id=user.id,
        tenant_id=user.tenant_id,
        device_token=token,
        platform=DevicePlatform.ANDROID,
    )
    db.add(device)
    db.commit()
    return device


def _make_escalation(db, tenant_id, *, priority=TicketPriority.HIGH, reason="Refund request"):
    session = ConversationSession(
        tenant_id=tenant_id,
        user_identifier=f"visitor:{uuid.uuid4().hex[:8]}",
        status=SessionStatus.ESCALATED,
        escalation_reason=reason,
        escalated_at=utcnow_naive() - timedelta(minutes=4),
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    ticket = Ticket(
        tenant_id=tenant_id,
        session_id=session.id,
        ticket_number=f"TKT-TEST-{uuid.uuid4().hex[:6]}",
        summary="Customer wants a refund",
        priority=priority,
        status=TicketStatus.OPEN,
    )
    db.add(ticket)
    db.commit()
    db.refresh(ticket)
    return ticket, session


def test_broadcast_excludes_away_and_deviceless(db):
    tenant_id = uuid.uuid4()
    ready = _make_user(db, tenant_id, "ready@x.com", UserRole.SUPPORT_AGENT)
    _make_device(db, ready, "tok-ready")
    away = _make_user(db, tenant_id, "away@x.com", UserRole.SUPPORT_AGENT, AvailabilityStatus.AWAY)
    _make_device(db, away, "tok-away")
    _make_user(db, tenant_id, "nodevice@x.com", UserRole.SUPPORT_AGENT)
    _make_user(db, tenant_id, "viewer@x.com", UserRole.VIEWER)

    ids = {u.id for u in AgentAlertService(db).broadcast_recipients(tenant_id)}
    assert ids == {ready.id}


def test_broadcast_is_tenant_scoped(db):
    mine, theirs = uuid.uuid4(), uuid.uuid4()
    a = _make_user(db, mine, "a@x.com", UserRole.SUPPORT_AGENT)
    _make_device(db, a, "tok-a")
    b = _make_user(db, theirs, "b@y.com", UserRole.SUPPORT_AGENT)
    _make_device(db, b, "tok-b")

    ids = {u.id for u in AgentAlertService(db).broadcast_recipients(mine)}
    assert ids == {a.id}


def test_supervisors_are_admins_regardless_of_availability(db):
    tenant_id = uuid.uuid4()
    admin = _make_user(db, tenant_id, "boss@x.com", UserRole.TENANT_ADMIN, AvailabilityStatus.AWAY)
    _make_device(db, admin, "tok-boss")
    agent = _make_user(db, tenant_id, "agent@x.com", UserRole.SUPPORT_AGENT)
    _make_device(db, agent, "tok-agent")

    ids = {u.id for u in AgentAlertService(db).supervisor_recipients(tenant_id)}
    assert ids == {admin.id}


def test_alert_payload_shape(db):
    tenant_id = uuid.uuid4()
    ticket, session = _make_escalation(db, tenant_id, priority=TicketPriority.URGENT)
    payload = AgentAlertService(db).build_alert_payload(ticket, session)

    assert payload["type"] == "escalation_alert"
    assert payload["escalation_id"] == str(ticket.id)
    assert payload["session_id"] == str(session.id)
    assert payload["priority"] == "critical"
    assert payload["reason"] == "Refund request"
    assert payload["customer_name"] == "Customer"
    assert int(payload["wait_minutes"]) >= 4
    assert all(isinstance(v, str) for v in payload.values())


@pytest.mark.parametrize(
    "priority,expected",
    [
        (TicketPriority.URGENT, "critical"),
        (TicketPriority.HIGH, "important"),
        (TicketPriority.MEDIUM, "normal"),
        (TicketPriority.LOW, "normal"),
    ],
)
def test_priority_mapping(db, priority, expected):
    tenant_id = uuid.uuid4()
    ticket, session = _make_escalation(db, tenant_id, priority=priority)
    assert AgentAlertService(db).build_alert_payload(ticket, session)["priority"] == expected


def test_claim_log_is_idempotent_per_tier(db):
    tenant_id = uuid.uuid4()
    ticket, _ = _make_escalation(db, tenant_id)
    user = _make_user(db, tenant_id, "a@x.com", UserRole.TENANT_ADMIN)
    svc = AgentAlertService(db)

    first = svc.claim_log(tenant_id, NotifEvent.ESCALATION_ALERT, ticket.id, user.id)
    second = svc.claim_log(tenant_id, NotifEvent.ESCALATION_ALERT, ticket.id, user.id)
    other_tier = svc.claim_log(tenant_id, NotifEvent.ESCALATION_FALLBACK, ticket.id, user.id)

    assert first is not None
    assert second is None, "same tier must dedupe"
    assert other_tier is not None, "fallback tier must NOT be deduped by the tier-1 row"


def test_rung_user_ids_covers_both_tiers(db):
    tenant_id = uuid.uuid4()
    ticket, _ = _make_escalation(db, tenant_id)
    a = _make_user(db, tenant_id, "a@x.com", UserRole.SUPPORT_AGENT)
    b = _make_user(db, tenant_id, "b@x.com", UserRole.TENANT_ADMIN)
    svc = AgentAlertService(db)
    svc.claim_log(tenant_id, NotifEvent.ESCALATION_ALERT, ticket.id, a.id)
    svc.claim_log(tenant_id, NotifEvent.ESCALATION_FALLBACK, ticket.id, b.id)

    assert set(svc.rung_user_ids(ticket.id)) == {a.id, b.id}


def test_prune_token_removes_the_device(db):
    tenant_id = uuid.uuid4()
    user = _make_user(db, tenant_id, "a@x.com", UserRole.SUPPORT_AGENT)
    _make_device(db, user, "tok-dead")
    AgentAlertService(db).prune_token(user.id, "tok-dead")
    assert AgentAlertService(db).devices_for([user.id], tenant_id) == []


def test_prune_token_does_not_remove_another_agents_device_with_the_same_token(db):
    """F6 regression: `agent_devices` is unique on (user_id, device_token), not
    device_token alone — a device handed over from one agent to another (the
    old agent never explicitly unregistering) can leave the same FCM token on
    two rows. An UNREGISTERED report for one agent's row must not silently
    unregister the other agent's identical token."""
    tenant_id = uuid.uuid4()
    handed_over_from = _make_user(db, tenant_id, "old@x.com", UserRole.SUPPORT_AGENT)
    handed_over_to = _make_user(db, tenant_id, "new@x.com", UserRole.SUPPORT_AGENT)
    _make_device(db, handed_over_from, "tok-shared")
    _make_device(db, handed_over_to, "tok-shared")

    AgentAlertService(db).prune_token(handed_over_from.id, "tok-shared")

    assert AgentAlertService(db).devices_for([handed_over_from.id], tenant_id) == []
    assert len(AgentAlertService(db).devices_for([handed_over_to.id], tenant_id)) == 1


def test_devices_for_is_tenant_scoped(db):
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    user_a = _make_user(db, tenant_a, "a@x.com", UserRole.SUPPORT_AGENT)
    _make_device(db, user_a, "tok-a")
    user_b = _make_user(db, tenant_b, "b@y.com", UserRole.SUPPORT_AGENT)
    _make_device(db, user_b, "tok-b")

    devices = AgentAlertService(db).devices_for([user_a.id, user_b.id], tenant_a)

    assert {d.user_id for d in devices} == {user_a.id}
