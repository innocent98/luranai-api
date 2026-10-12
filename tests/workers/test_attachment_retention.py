"""Daily attachment-retention job."""

from datetime import UTC, datetime, timedelta
import pytest
from unittest.mock import patch
from uuid import uuid4

from app.db.models.conversation import (
    AttachmentKind,
    AttachmentStatus,
    MessageAttachment,
    MessageRole,
    Ticket,
    TicketPriority,
    TicketStatus,
)
from app.services.conversation_service import ConversationService
from tests.workers.test_attachment_cleanup import _att, _NoCloseSession, _session

DESTROY = "app.workers.attachment_retention.cloudinary_manager.destroy"


def _linked(db, s, pid, *, expires_at, kind=AttachmentKind.IMAGE):
    msg = ConversationService(db).add_message(s.id, MessageRole.USER, "hi")
    return _att(
        db,
        s,
        pid,
        status=AttachmentStatus.LINKED,
        message_id=msg.id,
        expires_at=expires_at,
        kind=kind,
    )


def _ticket(db, s, status):
    t = Ticket(
        tenant_id=s.tenant_id,
        session_id=s.id,
        ticket_number=f"T-{uuid4().hex[:6]}",
        priority=TicketPriority.MEDIUM,
        status=status,
        summary="x",
    )
    db.add(t)
    db.commit()


async def _run(db, monkeypatch, **destroy_kw):
    import app.workers.attachment_retention as mod

    monkeypatch.setattr(mod, "SessionLocal", lambda: _NoCloseSession(db))
    with patch(DESTROY, **destroy_kw) as d:
        n = await mod.expire_attachments({})
    return n, d


async def test_expires_due_rows_keeps_row_nulls_urls(db, monkeypatch):
    s = _session(db)
    now = datetime.now(UTC)
    due = _linked(db, s, "a/due", expires_at=now - timedelta(hours=1), kind=AttachmentKind.VIDEO)
    future = _linked(db, s, "a/future", expires_at=now + timedelta(days=1))
    due_id, future_id = due.id, future.id
    n, d = await _run(db, monkeypatch, return_value=(True, None))
    assert n == 1
    d.assert_called_once_with("a/due", resource_type="video")
    db.expire_all()
    row = db.get(MessageAttachment, due_id)
    assert row.status == AttachmentStatus.EXPIRED and row.url is None and row.poster_url is None
    assert db.get(MessageAttachment, future_id).status == AttachmentStatus.LINKED


@pytest.mark.parametrize(
    "status", [TicketStatus.OPEN, TicketStatus.IN_PROGRESS, TicketStatus.MISSED]
)
async def test_active_ticket_defers_until_ceiling(db, monkeypatch, status):
    s = _session(db)
    _ticket(db, s, status)
    now = datetime.now(UTC)
    recent = _linked(db, s, "a/recent", expires_at=now - timedelta(days=1))
    stale = _linked(db, s, "a/stale", expires_at=now - timedelta(days=8))
    recent_id, stale_id = recent.id, stale.id
    n, d = await _run(db, monkeypatch, return_value=(True, None))
    assert n == 1
    d.assert_called_once_with("a/stale", resource_type="image")
    db.expire_all()
    assert db.get(MessageAttachment, recent_id).status == AttachmentStatus.LINKED
    assert db.get(MessageAttachment, stale_id).status == AttachmentStatus.EXPIRED


async def test_ticket_on_other_session_does_not_defer(db, monkeypatch):
    s1, s2 = _session(db), _session(db)
    _ticket(db, s2, TicketStatus.OPEN)
    _linked(db, s1, "a/x", expires_at=datetime.now(UTC) - timedelta(days=1))
    n, _ = await _run(db, monkeypatch, return_value=(True, None))
    assert n == 1


async def test_already_expired_not_destroyed_again(db, monkeypatch):
    s = _session(db)
    _att(
        db,
        s,
        "a/old",
        status=AttachmentStatus.EXPIRED,
        expires_at=datetime.now(UTC) - timedelta(days=3),
    )
    n, d = await _run(db, monkeypatch, return_value=(True, None))
    assert n == 0
    d.assert_not_called()


async def test_commit_error_is_isolated(db, monkeypatch):
    s = _session(db)
    now = datetime.now(UTC)
    _linked(db, s, "a/first", expires_at=now - timedelta(hours=3))
    second = _linked(db, s, "a/second", expires_at=now - timedelta(hours=2))
    second_id = second.id
    real_commit = db.commit
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("commit boom")
        return real_commit()

    rollbacks = []
    # The test fixture wraps one outer transaction, so a real rollback() would wipe the
    # seeded rows; record the call instead.
    monkeypatch.setattr(db, "commit", flaky)
    monkeypatch.setattr(db, "rollback", lambda: rollbacks.append(1))
    n, d = await _run(db, monkeypatch, return_value=(True, None))
    assert n == 1
    assert d.call_count == 2
    assert rollbacks == [1]
    db.expire_all()
    assert db.get(MessageAttachment, second_id).status == AttachmentStatus.EXPIRED


async def test_row_deleted_mid_run_does_not_abort(db, monkeypatch):
    s = _session(db)
    now = datetime.now(UTC)
    _linked(db, s, "a/first", expires_at=now - timedelta(hours=3))
    second = _linked(db, s, "a/second", expires_at=now - timedelta(hours=2))
    third = _linked(db, s, "a/third", expires_at=now - timedelta(hours=1))
    second_id, third_id = second.id, third.id

    def fake(public_id, *, resource_type):
        if public_id == "a/first":  # purge removes the next row mid-run
            db.query(MessageAttachment).filter_by(id=second_id).delete()
            db.commit()
        return (True, None)

    n, _ = await _run(db, monkeypatch, side_effect=fake)
    assert n == 2  # first + third; vanished second is skipped, not fatal
    db.expire_all()
    assert db.get(MessageAttachment, third_id).status == AttachmentStatus.EXPIRED


async def test_breaker_stops_run_after_consecutive_failures(db, monkeypatch):
    import app.workers.attachment_retention as mod

    s = _session(db)
    base = datetime.now(UTC) - timedelta(days=1)
    for i in range(25):
        _linked(db, s, f"a/{i}", expires_at=base + timedelta(seconds=i))
    n, d = await _run(db, monkeypatch, return_value=(False, "boom"))
    assert n == 0
    assert d.call_count == mod.MAX_CONSECUTIVE_FAILURES == 20


async def test_resolved_ticket_does_not_defer(db, monkeypatch):
    s = _session(db)
    _ticket(db, s, TicketStatus.RESOLVED)
    _linked(db, s, "a/x", expires_at=datetime.now(UTC) - timedelta(hours=1))
    n, _ = await _run(db, monkeypatch, return_value=(True, None))
    assert n == 1


async def test_failing_destroy_is_isolated_and_terminates(db, monkeypatch):
    s = _session(db)
    past = datetime.now(UTC) - timedelta(hours=1)
    bad = _linked(db, s, "a/bad", expires_at=past)
    good = _linked(db, s, "a/good", expires_at=past)
    bad_id, good_id = bad.id, good.id

    def fake(public_id, *, resource_type):
        return (False, "boom") if public_id == "a/bad" else (True, None)

    n, d = await _run(db, monkeypatch, side_effect=fake)
    assert n == 1
    assert d.call_count == 2  # bad tried once, not retried in a loop
    db.expire_all()
    assert db.get(MessageAttachment, bad_id).status == AttachmentStatus.LINKED
    assert db.get(MessageAttachment, good_id).status == AttachmentStatus.EXPIRED


async def test_kill_switch(db, monkeypatch):
    import app.workers.attachment_retention as mod

    monkeypatch.setattr(mod.settings, "ATTACHMENT_RETENTION_ENABLED", False)
    s = _session(db)
    _linked(db, s, "a/x", expires_at=datetime.now(UTC) - timedelta(hours=1))
    n, d = await _run(db, monkeypatch, return_value=(True, None))
    assert n == 0
    d.assert_not_called()


async def test_uploaded_rows_ignored(db, monkeypatch):
    s = _session(db)
    _att(
        db,
        s,
        "a/up",
        status=AttachmentStatus.UPLOADED,
        expires_at=datetime.now(UTC) - timedelta(hours=1),
    )
    n, d = await _run(db, monkeypatch, return_value=(True, None))
    assert n == 0  # unsent uploads belong to cleanup_orphan_attachments


LIST = "app.workers.attachment_retention.cloudinary_manager.list_tagged_videos"


def _vid(s, hexid=None):
    from app.core.config import settings

    return f"{settings.CLOUDINARY_CHAT_FOLDER}/{s.tenant_id}/{s.id}/video/{hexid or uuid4().hex}"


def _old():
    return (datetime.now(UTC) - timedelta(hours=30)).strftime("%Y-%m-%dT%H:%M:%SZ")


async def test_sweep_destroys_old_unregistered_only(db, monkeypatch):
    import app.workers.attachment_retention as mod

    monkeypatch.setattr(mod, "SessionLocal", lambda: _NoCloseSession(db))
    s = _session(db)
    registered, orphan, fresh = _vid(s), _vid(s), _vid(s)
    _att(db, s, registered, status=AttachmentStatus.UPLOADED, kind=AttachmentKind.VIDEO)
    new = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    pages = [
        (
            [
                {"public_id": registered, "created_at": _old()},
                {"public_id": orphan, "created_at": _old()},
            ],
            "c2",
        ),
        ([{"public_id": fresh, "created_at": new}], None),
    ]
    with patch(LIST, side_effect=pages) as lst, patch(DESTROY, return_value=(True, None)) as d:
        n = await mod.sweep_unregistered_videos({})
    assert n == 1
    d.assert_called_once_with(orphan, resource_type="video")
    assert lst.call_count == 2
    assert lst.call_args_list[0].args[0] == "screen_recording_" + mod.settings.ENVIRONMENT


async def test_sweep_skips_foreign_unparseable_unknown_and_tenant_mismatch(db, monkeypatch):
    import app.workers.attachment_retention as mod

    monkeypatch.setattr(mod, "SessionLocal", lambda: _NoCloseSession(db))
    s = _session(db)
    good = _vid(s)
    unknown_session = (
        f"{mod.settings.CLOUDINARY_CHAT_FOLDER}/{s.tenant_id}/{uuid4()}/video/{uuid4().hex}"
    )
    wrong_tenant = f"{mod.settings.CLOUDINARY_CHAT_FOLDER}/{uuid4()}/{s.id}/video/{uuid4().hex}"
    foreign_folder = f"other/folder/{s.tenant_id}/{s.id}/video/{uuid4().hex}"
    upper = _vid(s).upper()
    bad_hex = _vid(s, "abc")
    pages = [
        (
            [
                {"public_id": pid, "created_at": _old()}
                for pid in (
                    good,
                    unknown_session,
                    wrong_tenant,
                    foreign_folder,
                    upper,
                    bad_hex,
                    "v/orphan",
                )
            ],
            None,
        )
    ]
    with patch(LIST, side_effect=pages), patch(DESTROY, return_value=(True, None)) as d:
        n = await mod.sweep_unregistered_videos({})
    assert n == 1
    d.assert_called_once_with(good, resource_type="video")


async def test_sweep_tolerates_resource_without_public_id(db, monkeypatch):
    import app.workers.attachment_retention as mod

    monkeypatch.setattr(mod, "SessionLocal", lambda: _NoCloseSession(db))
    s = _session(db)
    good = _vid(s)
    pages = [
        (
            [
                {"created_at": _old()},
                {"public_id": None, "created_at": _old()},
                {"public_id": 5, "created_at": _old()},
                {"public_id": good, "created_at": _old()},
            ],
            None,
        )
    ]
    with patch(LIST, side_effect=pages), patch(DESTROY, return_value=(True, None)) as d:
        n = await mod.sweep_unregistered_videos({})
    assert n == 1
    d.assert_called_once_with(good, resource_type="video")


async def test_sweep_stops_at_page_cap(db, monkeypatch):
    import app.workers.attachment_retention as mod

    monkeypatch.setattr(mod, "SessionLocal", lambda: _NoCloseSession(db))
    with patch(LIST, return_value=([], "again")) as lst, patch(DESTROY):
        await mod.sweep_unregistered_videos({})
    assert lst.call_count == mod.SWEEP_MAX_PAGES


async def test_sweep_tolerates_list_exception(db, monkeypatch):
    import app.workers.attachment_retention as mod

    monkeypatch.setattr(mod, "SessionLocal", lambda: _NoCloseSession(db))
    s = _session(db)
    orphan = _vid(s)
    old = _old()
    pages = [
        ([{"public_id": orphan, "created_at": old}], "c2"),
        RuntimeError("cloudinary down"),
    ]
    with patch(LIST, side_effect=pages) as lst, patch(DESTROY, return_value=(True, None)) as d:
        n = await mod.sweep_unregistered_videos({})
    assert n == 1
    d.assert_called_once_with(orphan, resource_type="video")
    assert lst.call_count == 2
