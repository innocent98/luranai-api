"""Re-escalating an already-escalated session must not restart the clock."""

import uuid

from app.db.models.conversation import ConversationSession, SessionStatus
from app.services.conversation_service import ConversationService


def _session(db, tenant_id):
    session = ConversationSession(
        tenant_id=tenant_id,
        user_identifier=f"visitor-{uuid.uuid4().hex[:8]}",
        status=SessionStatus.ACTIVE,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def test_first_escalation_sets_reason_and_timestamp(db):
    session = _session(db, uuid.uuid4())
    ConversationService(db).escalate_session(session.id, "Refund request")
    db.refresh(session)
    assert session.status == SessionStatus.ESCALATED
    assert session.escalation_reason == "Refund request"
    assert session.escalated_at is not None


def test_second_escalation_preserves_the_original(db):
    session = _session(db, uuid.uuid4())
    svc = ConversationService(db)
    svc.escalate_session(session.id, "Refund request")
    db.refresh(session)
    original_at, original_reason = session.escalated_at, session.escalation_reason

    svc.escalate_session(session.id, "Still angry")
    db.refresh(session)
    assert session.escalated_at == original_at
    assert session.escalation_reason == original_reason
