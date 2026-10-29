"""The shared conversation count, limit check, reuse window and safe getters."""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core.config import settings
from app.db.models.conversation import SessionStatus
from app.db.models.tenant import TenantCapabilities
from app.services import conversation_counting as cc
from app.services.conversation_service import ConversationService
from tests.conversation_counting.conftest import seed_session
from tests.whatsapp.conftest import make_tenant

pytestmark = pytest.mark.filterwarnings("ignore")


def _stale(status, hours_ago, message=None):
    from app.utils.datetime import utcnow_naive

    session = SimpleNamespace(
        status=status, last_message_at=utcnow_naive() - timedelta(hours=hours_ago)
    )
    return cc.is_stale_closed(session, message)


# ------------------------------------------------------------------ window boundary
def test_default_window_is_six_hours():
    assert settings.CONVERSATION_REUSE_WINDOW_HOURS == 6


@pytest.mark.parametrize("status", [SessionStatus.ABANDONED, SessionStatus.RESOLVED])
def test_closed_session_just_inside_the_window_is_continued(status):
    assert _stale(status, 5.99) is False


@pytest.mark.parametrize("status", [SessionStatus.ABANDONED, SessionStatus.RESOLVED])
def test_closed_session_just_outside_the_window_is_stale(status):
    assert _stale(status, 6.02) is True


@pytest.mark.parametrize("status", [SessionStatus.ACTIVE, SessionStatus.ESCALATED])
def test_open_sessions_are_never_stale(status):
    assert _stale(status, 72) is False


def test_resolved_plus_greeting_is_not_a_new_conversation():
    assert _stale(SessionStatus.RESOLVED, 72, "__init__") is False
    assert _stale(SessionStatus.ABANDONED, 72, "start") is True  # abandoned always reactivates


def test_aware_timestamps_from_postgres_are_compared_in_utc():
    from datetime import UTC
    from datetime import datetime as dt

    from app.utils.datetime import utcnow_naive

    recent = utcnow_naive().replace(tzinfo=UTC) - timedelta(hours=5.9)
    old = utcnow_naive().replace(tzinfo=UTC) - timedelta(hours=6.1)
    assert (
        cc.is_stale_closed(SimpleNamespace(status=SessionStatus.RESOLVED, last_message_at=recent))
        is False
    )
    assert (
        cc.is_stale_closed(SimpleNamespace(status=SessionStatus.RESOLVED, last_message_at=old))
        is True
    )
    assert isinstance(recent, dt)


def test_window_is_one_setting(monkeypatch):
    monkeypatch.setattr(settings, "CONVERSATION_REUSE_WINDOW_HOURS", 1)
    assert _stale(SessionStatus.RESOLVED, 1.5) is True
    assert _stale(SessionStatus.RESOLVED, 0.5) is False


def test_reopen_flips_the_same_row_and_leaves_greetings_alone():
    resolved = SimpleNamespace(status=SessionStatus.RESOLVED)
    assert cc.reopen_if_closed(resolved, "__init__") is False
    assert resolved.status == SessionStatus.RESOLVED
    assert cc.reopen_if_closed(resolved, "one more thing") is True
    assert resolved.status == SessionStatus.ACTIVE
    abandoned = SimpleNamespace(status=SessionStatus.ABANDONED)
    assert cc.reopen_if_closed(abandoned, "__init__") is True
    assert abandoned.status == SessionStatus.ACTIVE


# ------------------------------------------------------------------ count + month rollover
def test_count_is_per_tenant_and_this_month_only(db, tenant):
    other = make_tenant(db)
    now = datetime(2026, 11, 15, 12, 0, 0)
    for created in (datetime(2026, 11, 1, 0, 0, 0), datetime(2026, 11, 14, 9, 0, 0)):
        row = seed_session(db, tenant.id)
        row.created_at = created
    last_month = seed_session(db, tenant.id)
    last_month.created_at = datetime(2026, 10, 31, 23, 59, 59)
    seed_session(db, other.id).created_at = datetime(2026, 11, 3, 0, 0, 0)
    db.commit()
    assert cc.count_conversations_this_month(db, tenant.id, now=now) == 2
    # Rolled over: on 1 Dec the November rows no longer count.
    assert cc.count_conversations_this_month(db, tenant.id, now=datetime(2026, 12, 1)) == 0


def test_resets_at_is_the_first_of_next_month_and_year_rolls(db, tenant):
    usage = cc.conversation_usage(db, tenant.id, now=datetime(2026, 12, 20, 8, 0, 0))
    assert usage.resets_at == datetime(2027, 1, 1)
    usage = cc.conversation_usage(db, tenant.id, now=datetime(2026, 1, 31, 23, 0, 0))
    assert usage.resets_at == datetime(2026, 2, 1)


def _set_limit(db, tenant, value):
    caps = db.query(TenantCapabilities).filter_by(tenant_id=tenant.id).one()
    caps.max_conversations_per_month = value
    db.commit()


def test_enforce_raises_the_402_body_clients_already_parse(db, tenant):
    seed_session(db, tenant.id)
    seed_session(db, tenant.id)
    _set_limit(db, tenant, 2)
    with pytest.raises(HTTPException) as exc:
        cc.enforce_conversation_limit(db, tenant.id)
    assert exc.value.status_code == 402
    detail = exc.value.detail
    assert set(detail) == {
        "error",
        "message",
        "definition",
        "current_count",
        "limit",
        "resets_at",
    }
    assert detail["definition"] == cc.conversation_definition()
    assert detail["error"] == "conversation_limit_reached"
    assert detail["message"] == "Monthly conversation limit of 2 reached."
    assert detail["current_count"] == 2 and detail["limit"] == 2
    assert datetime.fromisoformat(detail["resets_at"]).day == 1


def test_enforce_passes_under_the_cap_and_when_unlimited(db, tenant):
    seed_session(db, tenant.id)
    _set_limit(db, tenant, 2)
    cc.enforce_conversation_limit(db, tenant.id)  # 1 < 2
    _set_limit(db, tenant, None)
    cc.enforce_conversation_limit(db, tenant.id)
    assert cc.conversation_usage(db, tenant.id) is None


def test_no_capabilities_row_means_no_cap(db):
    from uuid import uuid4

    assert cc.conversation_limit(db, uuid4()) is None
    assert cc.conversation_limit_reached(db, uuid4()) is False


# ------------------------------------------------------------------ getters
def test_visitor_getter_returns_the_latest_among_many_rows(db, tenant, make_visitor):
    visitor = make_visitor()
    seed_session(db, tenant.id, visitor_id=visitor.id, hours_ago=3)
    newest = seed_session(db, tenant.id, visitor_id=visitor.id, hours_ago=1)
    seed_session(db, tenant.id, visitor_id=visitor.id, hours_ago=2)
    found = ConversationService(db).get_latest_session_for_visitor(tenant.id, visitor.id)
    assert found.id == newest.id  # was MultipleResultsFound


def test_customer_getter_returns_the_latest_among_many_rows(db, tenant):
    from uuid import uuid4

    customer_id = uuid4()
    seed_session(db, tenant.id, customer_user_id=customer_id, hours_ago=5)
    newest = seed_session(db, tenant.id, customer_user_id=customer_id, hours_ago=0.5)
    found = ConversationService(db).get_latest_session_for_customer(tenant.id, customer_id)
    assert found.id == newest.id


def test_active_for_user_getter_survives_duplicates(db, tenant):
    seed_session(db, tenant.id, user_identifier="same", hours_ago=2)
    newest = seed_session(db, tenant.id, user_identifier="same", hours_ago=1)
    assert ConversationService(db).get_active_session_for_user(tenant.id, "same").id == newest.id


def test_reuse_window_filter_on_the_getter(db, tenant, make_visitor):
    svc = ConversationService(db)
    visitor = make_visitor()
    old_resolved = seed_session(
        db, tenant.id, status=SessionStatus.RESOLVED, hours_ago=6.5, visitor_id=visitor.id
    )
    assert svc.get_latest_session_for_visitor(tenant.id, visitor.id, reuse_window=True) is None
    assert svc.get_latest_session_for_visitor(tenant.id, visitor.id).id == old_resolved.id
    # an escalated thread is continued whatever its age
    escalated = seed_session(
        db, tenant.id, status=SessionStatus.ESCALATED, hours_ago=48, visitor_id=visitor.id
    )
    found = svc.get_latest_session_for_visitor(tenant.id, visitor.id, reuse_window=True)
    assert found.id == escalated.id
    # a recent abandoned one wins over an older escalated one
    recent = seed_session(
        db, tenant.id, status=SessionStatus.ABANDONED, hours_ago=1, visitor_id=visitor.id
    )
    found = svc.get_latest_session_for_visitor(tenant.id, visitor.id, reuse_window=True)
    assert found.id == recent.id
