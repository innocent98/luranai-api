from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from redis.exceptions import RedisError
from sqlalchemy import select

from app.core.config import settings
from app.db.models.conversation import ConversationSession, SessionStatus
from app.db.models.conversation import Ticket as TicketModel
from app.db.models.tenant import TenantCapabilities


@pytest.fixture(autouse=True)
def _voice_secret(monkeypatch):
    monkeypatch.setattr(settings, "VOICE_SHARED_SECRET", "s3cret")


def _auth():
    return {"Authorization": "Bearer s3cret"}


def test_escalation_marks_session_and_returns_switch_signal(
    pg_client, db_session, voice_tenant_factory, voice_session_factory, minted_voice_token
):
    tenant = voice_tenant_factory(can_use_voice=True, max_voice_minutes_per_month=None)
    session = voice_session_factory(tenant.id)
    token = minted_voice_token(tenant.id, session.id)

    resp = pg_client.post(
        "/api/v1/voice/tools/escalate_to_human",
        json={"voice_token": token, "reason": "customer asked for a person"},
        headers=_auth(),
    )

    assert resp.status_code == 200
    assert resp.json()["switch_to_text"] is True
    assert resp.json()["ticket_id"]

    db_session.expire_all()
    refreshed = db_session.get(ConversationSession, session.id)
    assert refreshed.status == SessionStatus.ESCALATED


def test_escalation_requires_valid_voice_token(pg_client):
    resp = pg_client.post(
        "/api/v1/voice/tools/escalate_to_human",
        json={"voice_token": "bogus", "reason": "x"},
        headers=_auth(),
    )
    assert resp.status_code == 401


def test_escalation_is_idempotent_for_one_session(
    pg_client, db_session, voice_tenant_factory, voice_session_factory, minted_voice_token
):
    """A repeated tool call must not open a second ticket."""
    tenant = voice_tenant_factory(can_use_voice=True, max_voice_minutes_per_month=None)
    session = voice_session_factory(tenant.id)
    token = minted_voice_token(tenant.id, session.id)
    body = {"voice_token": token, "reason": "help"}

    first = pg_client.post("/api/v1/voice/tools/escalate_to_human", json=body, headers=_auth())
    second = pg_client.post("/api/v1/voice/tools/escalate_to_human", json=body, headers=_auth())

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["ticket_id"] == second.json()["ticket_id"]

    db_session.expire_all()
    tickets = (
        db_session.execute(select(TicketModel).where(TicketModel.session_id == session.id))
        .scalars()
        .all()
    )
    assert len(tickets) == 1


def test_resolve_failure_still_returns_degraded_response_not_500(
    pg_client, voice_tenant_factory, voice_session_factory, minted_voice_token
):
    """A Redis outage (or any other failure) during token resolution happens
    before we even know who the caller is, so it can't get a real ticket —
    but it must never surface as an unhandled 500. This is the same
    silence-avoidance contract `voice_custom_llm` already proves for its own
    `resolve()` call via `test_resolve_failure_still_returns_speakable_audio`
    in test_custom_llm.py; this is that test's counterpart for the JSON
    (non-SSE) escalate route."""
    tenant = voice_tenant_factory(can_use_voice=True, max_voice_minutes_per_month=None)
    session = voice_session_factory(tenant.id)
    token = minted_voice_token(tenant.id, session.id)

    with patch(
        "app.api.v1.endpoints.voice.VoiceSessionTokenStore.resolve",
        new=AsyncMock(side_effect=RedisError("redis unreachable")),
    ):
        resp = pg_client.post(
            "/api/v1/voice/tools/escalate_to_human",
            json={"voice_token": token, "reason": "help"},
            headers=_auth(),
        )

    assert resp.status_code == 200
    assert resp.json() == {"switch_to_text": True, "ticket_id": None}


def test_ticket_creation_disabled_for_tenant_still_returns_degraded_response_not_500(
    pg_client, db_session, voice_tenant_factory, voice_session_factory, minted_voice_token
):
    """`TicketService.create_ticket` raises `ValueError` when the tenant's
    `TenantCapabilities.can_create_tickets` is False (ticket_service.py:110-114)
    — a real, reachable tenant configuration, not a contrived mock. The
    endpoint must degrade to a speakable-by-the-agent 200 rather than an
    unhandled 500 in that case too."""
    tenant = voice_tenant_factory(can_use_voice=True, max_voice_minutes_per_month=None)
    capabilities = db_session.execute(
        select(TenantCapabilities).where(TenantCapabilities.tenant_id == tenant.id)
    ).scalar_one()
    capabilities.can_create_tickets = False
    db_session.commit()
    session = voice_session_factory(tenant.id)
    token = minted_voice_token(tenant.id, session.id)

    resp = pg_client.post(
        "/api/v1/voice/tools/escalate_to_human",
        json={"voice_token": token, "reason": "help"},
        headers=_auth(),
    )

    assert resp.status_code == 200
    assert resp.json() == {"switch_to_text": True, "ticket_id": None}


def test_cross_tenant_fields_in_body_are_never_honoured(
    pg_client, db_session, voice_tenant_factory, voice_session_factory, minted_voice_token
):
    """Injecting tenant B's ids into the body alongside tenant A's real voice
    token must never leak into which tenant/session the ticket is created
    for. Only the token's resolved binding may ever reach `TicketCreate` —
    mirrors `test_cross_tenant_fields_in_body_are_never_honoured` in
    test_custom_llm.py, same injection shape, this route's own consumer of
    the resolved context."""
    tenant_a = voice_tenant_factory(can_use_voice=True, max_voice_minutes_per_month=None)
    tenant_b = voice_tenant_factory(can_use_voice=True, max_voice_minutes_per_month=None)
    session_a = voice_session_factory(tenant_a.id)
    session_b = voice_session_factory(tenant_b.id)
    token_a = minted_voice_token(tenant_a.id, session_a.id)

    body = {
        "voice_token": token_a,
        "reason": "help",
        "tenant_id": str(tenant_b.id),
        "session_id": str(session_b.id),
    }

    resp = pg_client.post("/api/v1/voice/tools/escalate_to_human", json=body, headers=_auth())

    assert resp.status_code == 200
    ticket_id = resp.json()["ticket_id"]
    assert ticket_id

    db_session.expire_all()
    ticket = db_session.get(TicketModel, UUID(ticket_id))
    assert ticket is not None
    assert ticket.tenant_id == tenant_a.id
    assert ticket.session_id == session_a.id
    assert ticket.tenant_id != tenant_b.id
    assert ticket.session_id != session_b.id
