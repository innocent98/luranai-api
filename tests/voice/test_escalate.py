import pytest
from sqlalchemy import select

from app.core.config import settings
from app.db.models.conversation import ConversationSession, SessionStatus
from app.db.models.conversation import Ticket as TicketModel


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
