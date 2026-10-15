"""Customer presence service: Redis-derived state, grace, state-change-only frames.

Uses the real Redis from REDIS_URL (as CI does) and an injected clock so TTL / grace
behaviour is deterministic. Each test uses a fresh session id, so keys never collide.
"""

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.core.websocket import connection_manager
from app.services import customer_presence as cp
from app.services.customer_presence import CustomerPresenceTracker, read_presence_many


class Clock:
    def __init__(self) -> None:
        self.t = 1_800_000_000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


@pytest.fixture()
def clock():
    return Clock()


@pytest.fixture()
def tracker(clock):
    return CustomerPresenceTracker(now=clock, conn_ttl_s=45, grace_s=30, keepalive_s=3600)


@pytest.fixture()
def frames(monkeypatch):
    sent: list[dict] = []

    async def _fanout(session_id, message, to_customer, to_agents, exclude_agent_id=None):
        assert to_agents and not to_customer
        sent.append(message)

    monkeypatch.setattr(connection_manager, "fanout_event", AsyncMock(side_effect=_fanout))
    return sent


def _read(db, sid, clock):
    return read_presence_many(db, [sid], now=clock)[sid]


async def test_connect_is_active_and_emits_one_frame(tracker, frames, db, clock):
    sid = uuid4()
    conn = tracker.open(sid)
    await conn.start()
    assert _read(db, sid, clock).state == "active"
    assert [f["state"] for f in frames] == ["active"]
    assert frames[0] == {
        "type": "customer_presence",
        "session_id": str(sid),
        "state": "active",
        "last_seen_at": None,
    }
    await conn.close()
    await tracker.aclose()


async def test_never_seen_session_is_offline_without_last_seen(db, clock):
    sid = uuid4()
    p = _read(db, sid, clock)
    assert p.state == "offline" and p.last_seen_at is None


async def test_away_then_back_emits_only_on_change(tracker, frames, clock):
    sid = uuid4()
    conn = tracker.open(sid)
    await conn.start()
    clock.advance(2)
    await conn.handle_frame({"type": "presence", "state": "away"})
    clock.advance(10)
    await conn.handle_frame({"type": "presence", "state": "away"})  # heartbeat, same state
    clock.advance(10)
    await conn.handle_frame({"type": "presence", "state": "away"})
    clock.advance(2)
    await conn.handle_frame({"type": "presence", "state": "active"})
    assert [f["state"] for f in frames] == ["active", "away", "active"]
    await conn.close()
    await tracker.aclose()


async def test_invalid_state_ignored(tracker, frames):
    conn = tracker.open(uuid4())
    await conn.start()
    await conn.handle_frame({"type": "presence", "state": "offline"})
    await conn.handle_frame({"type": "presence"})
    await conn.handle_frame({"type": "presence", "state": 5})
    assert [f["state"] for f in frames] == ["active"]
    await conn.close()
    await tracker.aclose()


async def test_multi_connection_aggregation(tracker, frames, db, clock):
    sid = uuid4()
    a, b = tracker.open(sid), tracker.open(sid)
    await a.start()
    await b.start()
    clock.advance(2)
    await a.handle_frame({"type": "presence", "state": "away"})
    assert _read(db, sid, clock).state == "active"  # b still active
    clock.advance(2)
    await b.handle_frame({"type": "presence", "state": "away"})
    assert _read(db, sid, clock).state == "away"  # all away
    clock.advance(2)
    await a.handle_frame({"type": "presence", "state": "active"})
    assert _read(db, sid, clock).state == "active"
    assert [f["state"] for f in frames] == ["active", "away", "active"]
    await a.close()
    await b.close()
    await tracker.aclose()


async def test_aggregation_across_workers(clock, frames, db):
    """Two trackers (== two API workers) share only Redis."""
    w1 = CustomerPresenceTracker(now=clock, keepalive_s=3600)
    w2 = CustomerPresenceTracker(now=clock, keepalive_s=3600)
    sid = uuid4()
    c1, c2 = w1.open(sid), w2.open(sid)
    await c1.start()
    await c2.start()
    clock.advance(2)
    await c1.handle_frame({"type": "presence", "state": "away"})
    assert _read(db, sid, clock).state == "active"
    clock.advance(2)
    await c2.handle_frame({"type": "presence", "state": "away"})
    assert _read(db, sid, clock).state == "away"
    assert [f["state"] for f in frames] == ["active", "away"]
    await c1.close()
    await c2.close()
    await w1.aclose()
    await w2.aclose()


async def test_grace_then_offline_persists_last_seen(clock, frames, db):
    persisted: list[tuple] = []
    tracker = CustomerPresenceTracker(now=clock, grace_s=0.05, keepalive_s=3600)
    sid = uuid4()
    conn = tracker.open(sid, lambda s, t: persisted.append((s, t)))
    await conn.start()
    await conn.close()
    # inside grace: prior state still reported, no offline frame yet
    assert _read(db, sid, clock).state == "active"
    assert [f["state"] for f in frames] == ["active"]
    clock.advance(0.1)  # grace elapsed on the injected clock
    await asyncio.sleep(0.7)  # let the scheduled check run
    assert [f["state"] for f in frames] == ["active", "offline"]
    assert frames[-1]["last_seen_at"] is not None
    assert persisted and persisted[0][0] == sid
    p = _read(db, sid, clock)
    assert p.state == "offline" and p.last_seen_at is not None
    await tracker.aclose()


async def test_reload_inside_grace_emits_nothing(tracker, frames, db, clock):
    sid = uuid4()
    first = tracker.open(sid)
    await first.start()
    await first.close()
    clock.advance(5)
    second = tracker.open(sid)
    await second.start()
    assert [f["state"] for f in frames] == ["active"]
    # the old grace check firing later must not flip it offline
    await tracker.recompute_and_publish(sid)
    assert _read(db, sid, clock).state == "active"
    assert [f["state"] for f in frames] == ["active"]
    await second.close()
    await tracker.aclose()


async def test_ttl_lapse_reads_offline_without_any_frame(tracker, frames, db, clock):
    """Worker died mid-connection: nobody refreshes, reads resolve to offline."""
    sid = uuid4()
    conn = tracker.open(sid)
    await conn.start()
    conn._keepalive.cancel()  # simulate the worker dying
    clock.advance(46)
    p = _read(db, sid, clock)
    assert p.state == "offline"
    assert p.last_seen_at is not None  # Redis `last` key survives the lapse
    assert [f["state"] for f in frames] == ["active"]  # missed offline frame is accepted


async def test_db_last_seen_used_when_redis_expired(db, clock):
    from app.db.models.conversation import ConversationSession
    from app.db.models.tenant import Tenant

    tenant = Tenant(
        name=f"t_{uuid4().hex[:8]}",
        display_name="T",
        api_key_hash=uuid4().hex,
        subscription_tier="pro",
        subscription_status="active",
    )
    db.add(tenant)
    db.flush()
    seen = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    sess = ConversationSession(
        tenant_id=tenant.id, user_identifier="u1", customer_last_seen_at=seen
    )
    db.add(sess)
    db.flush()
    p = _read(db, sess.id, clock)
    assert p.state == "offline"
    assert p.last_seen_at.replace(tzinfo=UTC) == seen


async def test_batch_read_is_single_pipeline(tracker, frames, db, clock, monkeypatch):
    ids = [uuid4() for _ in range(5)]
    conns = [tracker.open(i) for i in ids[:2]]
    for c in conns:
        await c.start()
    clock.advance(2)
    await conns[1].handle_frame({"type": "presence", "state": "away"})

    calls = {"n": 0}
    client = cp._get_sync_client()
    real = client.pipeline

    def counting(*a, **kw):
        calls["n"] += 1
        return real(*a, **kw)

    monkeypatch.setattr(client, "pipeline", counting)
    out = read_presence_many(db, ids, now=clock)
    assert calls["n"] == 1
    assert [out[i].state for i in ids] == ["active", "away", "offline", "offline", "offline"]
    for c in conns:
        await c.close()
    await tracker.aclose()


async def test_flood_guard_drops_rapid_flips(tracker, frames, clock):
    conn = tracker.open(uuid4())
    await conn.start()
    # A page that is already hidden when the socket opens must be able to say so at once.
    await conn.handle_frame({"type": "presence", "state": "away"})
    # An immediate flip back is dropped (<1 s since the last accepted frame).
    await conn.handle_frame({"type": "presence", "state": "active"})
    assert [f["state"] for f in frames] == ["active", "away"]
    clock.advance(2)
    await conn.handle_frame({"type": "presence", "state": "active"})
    assert [f["state"] for f in frames] == ["active", "away", "active"]
    await conn.close()
    await tracker.aclose()


async def test_redis_failure_never_raises(clock, monkeypatch):
    t = CustomerPresenceTracker(now=clock, keepalive_s=3600)

    async def boom(*a, **k):
        raise ConnectionError("redis down")

    monkeypatch.setattr(t, "set_conn", boom)
    conn = t.open(uuid4())
    await conn.start()  # must not raise
    await conn.handle_frame({"type": "presence", "state": "away"})
    await conn.close()
    await t.aclose()


def test_read_degrades_to_empty_when_redis_down(db, monkeypatch):
    import redis as r

    def boom():
        raise r.ConnectionError("down")

    monkeypatch.setattr(cp, "_get_sync_client", boom)
    # RedisError subclass raised while building the pipeline -> {}
    assert read_presence_many(db, [uuid4()]) == {}


# ---------------------------------------------------------------- review fix wave


async def test_cancelled_watch_then_return_emits_back_frame_and_keeps_last_seen(
    frames, db, monkeypatch
):
    """Deploy cancels release_and_watch: no offline frame, no DB write. The stale
    `pub` key must not swallow the customer's next `active` frame, and last_seen
    must still be readable (and get persisted lazily)."""
    from app.db.models.conversation import ConversationSession
    from app.db.models.tenant import Tenant

    monkeypatch.setattr(cp, "_PUB_SLACK_MS", 100)
    tenant = Tenant(
        name=f"t_{uuid4().hex[:8]}",
        display_name="T",
        api_key_hash=uuid4().hex,
        subscription_tier="pro",
        subscription_status="active",
    )
    db.add(tenant)
    db.flush()
    sess = ConversationSession(tenant_id=tenant.id, user_identifier="u-cancel")
    db.add(sess)
    db.flush()
    sid = sess.id

    t = CustomerPresenceTracker(grace_s=0.2, keepalive_s=3600)
    conn = t.open(sid)
    await conn.start()
    conn._keepalive.cancel()
    # what survives a cancelled handler: the release, but never the watch task
    await t.release_conn(sid, conn.conn_id)
    await asyncio.sleep(0.6)  # grace over, pub key expired with it

    p = read_presence_many(db, [sid])[sid]
    assert p.state == "offline" and p.last_seen_at is not None  # never null after a deploy
    db.refresh(sess)
    assert sess.customer_last_seen_at is not None  # persisted lazily on read

    back = t.open(sid)
    await back.start()
    assert [f["state"] for f in frames] == ["active", "active"]  # the back frame IS emitted
    await back.close()
    await t.aclose()


async def test_offline_transition_clears_pub_key(frames):
    t = CustomerPresenceTracker(grace_s=0.05, keepalive_s=3600)
    sid = uuid4()
    conn = t.open(sid)
    await conn.start()
    await conn.close()
    await asyncio.sleep(0.8)
    assert [f["state"] for f in frames] == ["active", "offline"]
    assert cp._get_sync_client().get(cp._pub_key(sid)) is None
    await t.aclose()


async def test_keepalive_keeps_socket_active_past_ttl_and_stops_on_close(frames, db, clock):
    t = CustomerPresenceTracker(now=clock, conn_ttl_s=45, grace_s=30, keepalive_s=0.05)
    sid = uuid4()
    conn = t.open(sid)
    await conn.start()
    clock.advance(40)
    await asyncio.sleep(0.2)  # keepalive pushes the entry to clock+45
    clock.advance(40)  # 80 s since connect: lapsed without the keepalive
    await asyncio.sleep(0.2)
    assert _read(db, sid, clock).state == "active"
    keepalive = conn._keepalive
    await conn.close()
    await asyncio.sleep(0.1)
    assert keepalive.done()
    await t.aclose()


async def test_rapid_hidden_then_visible_applies_trailing_state(frames, db, clock):
    t = CustomerPresenceTracker(now=clock, keepalive_s=3600)
    conn = t.open(uuid4())
    await conn.start()
    await conn.handle_frame({"type": "presence", "state": "away"})
    await conn.handle_frame({"type": "presence", "state": "active"})  # inside the 1 s window
    assert [f["state"] for f in frames] == ["active", "away"]
    await asyncio.sleep(1.4)  # trailing edge applies the latest dropped state
    assert [f["state"] for f in frames] == ["active", "away", "active"]
    await conn.close()
    await t.aclose()


def test_sync_client_has_socket_timeouts():
    kw = cp._get_sync_client().connection_pool.connection_kwargs
    assert 0 < kw["socket_timeout"] <= 1
    assert 0 < kw["socket_connect_timeout"] <= 1


def test_blackholed_redis_does_not_hang_reads(db, monkeypatch):
    import socket
    import time as _t

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)  # accepts the TCP handshake, never answers
    monkeypatch.setattr(cp.settings, "REDIS_URL", f"redis://127.0.0.1:{srv.getsockname()[1]}/0")
    monkeypatch.setattr(cp, "_sync_client", None)
    start = _t.time()
    assert read_presence_many(db, [uuid4()]) == {}
    assert _t.time() - start < 3
    srv.close()
    monkeypatch.setattr(cp, "_sync_client", None)


def test_lazy_persist_never_moves_last_seen_backwards(db):
    """A newer offline write landing between the read and the lazy write wins."""
    from app.db.models.conversation import ConversationSession
    from app.db.models.tenant import Tenant

    tenant = Tenant(
        name=f"t_{uuid4().hex[:8]}",
        display_name="T",
        api_key_hash=uuid4().hex,
        subscription_tier="pro",
        subscription_status="active",
    )
    db.add(tenant)
    db.flush()
    newer = datetime(2026, 10, 1, 12, 5, tzinfo=UTC)
    sess = ConversationSession(
        tenant_id=tenant.id, user_identifier="u-race", customer_last_seen_at=newer
    )
    db.add(sess)
    db.flush()
    older = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    # The read saw no DB value (stale snapshot) and an older Redis `last`.
    cp._persist_lazily(db, [sess.id], {sess.id: str(older.timestamp())}, {sess.id: None})
    db.refresh(sess)
    assert sess.customer_last_seen_at.replace(tzinfo=UTC) == newer


def test_aclose_on_a_new_loop_drops_a_client_bound_to_a_closed_loop():
    """Test clients run one event loop each; shutdown must not touch a dead loop's client."""
    t = CustomerPresenceTracker(grace_s=0.05, keepalive_s=3600)

    async def bind_client():
        await t._client()
        await (await t._client()).ping()

    asyncio.run(bind_client())  # this loop is now closed, the client is still bound to it
    asyncio.run(t.aclose())  # must not raise "Event loop is closed"
    assert t._redis is None
