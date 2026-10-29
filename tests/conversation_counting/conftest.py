"""Fixtures for conversation counting: a tenant with a cap, and the LLM stubbed out."""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest

from app.db.models.conversation import ConversationSession, SessionStatus
from app.db.models.visitor import Visitor, VisitorSource
from app.utils.datetime import utcnow_aware, utcnow_naive
from tests.whatsapp.conftest import llm, make_tenant  # noqa: F401  (llm is a fixture)


@pytest.fixture
def tenant(db):
    return make_tenant(db, max_conversations=1000)


@pytest.fixture
def make_visitor(db, tenant):
    def _make(tenant_id=None) -> Visitor:
        now = utcnow_aware()
        visitor = Visitor(
            tenant_id=tenant_id or tenant.id,
            public_token=uuid4().hex,
            first_seen_at=now,
            last_seen_at=now,
            created_from=VisitorSource.WIDGET,
        )
        db.add(visitor)
        db.commit()
        return visitor

    return _make


def seed_session(
    db,
    tenant_id,
    *,
    status=SessionStatus.ACTIVE,
    hours_ago: float = 0,
    created_hours_ago: float | None = None,
    visitor_id=None,
    customer_user_id=None,
    user_identifier=None,
) -> ConversationSession:
    """A session whose last message was `hours_ago` hours back (created then, unless overridden)."""
    now = utcnow_naive()
    row = ConversationSession(
        tenant_id=tenant_id,
        user_identifier=user_identifier or f"u-{uuid4().hex[:8]}",
        visitor_id=visitor_id,
        customer_user_id=customer_user_id,
        status=status,
        last_message_at=now - timedelta(hours=hours_ago),
    )
    db.add(row)
    db.flush()
    row.created_at = now - timedelta(
        hours=hours_ago if created_hours_ago is None else created_hours_ago
    )
    db.commit()
    return row
