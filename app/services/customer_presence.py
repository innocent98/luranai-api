"""Customer presence (active / away / offline) for chat sessions.

Redis is the source of truth so the answer is right across API workers:

* ``presence:conn:{session_id}`` is a ZSET, member ``"{conn_id}|{state}"``, score =
  the epoch second the entry stops counting. A live socket keeps pushing its score
  forward (``CONN_TTL_S``); a closed socket is pushed to ``now + GRACE_S`` instead of
  being deleted, so a reload inside the grace window never flaps. A worker that dies
  simply stops refreshing and its entries lapse on their own (reads filter by score).
* ``presence:pub:{session_id}`` is the last state *published* to agents. A Lua
  compare-and-set against it is what makes frames state-change-only, even when two
  workers recompute at the same instant.
* ``presence:last:{session_id}`` is the last epoch the customer was seen connected;
  the offline transition copies it to ``conversation_sessions.customer_last_seen_at``.

State is always *derived* on read (any active -> active, else any away -> away, else
offline); no stored "online" flag can go stale.
"""

import asyncio
import time
import uuid
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from uuid import UUID

import redis as sync_redis
import redis.asyncio as aioredis
from loguru import logger
from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.websocket import connection_manager
from app.schemas.presence import CustomerPresence

HEARTBEAT_S = 20  # client beat cadence (documented for FEs)
CONN_TTL_S = 45  # a connection entry that is not refreshed stops counting after this
GRACE_S = 30  # after the last socket closes, keep the prior state this long
KEEPALIVE_S = 15  # server-side refresh of this worker's open sockets
_KEY_TTL_S = 300  # GC for the per-session ZSET
_LAST_TTL_S = 7 * 86400  # outlives any deploy gap so last_seen can be persisted lazily
# `pub` (last published state) is liveness-scoped: it must not outlive the connection
# entries it describes, or a stale value swallows the next real change. Refreshed by every
# set_conn, shortened to the grace window on release, deleted on the offline transition.
_PUB_TTL_S = CONN_TTL_S + GRACE_S + 15
_PUB_SLACK_MS = 2000
_SOCKET_TIMEOUT_S = 0.75
_MIN_CHANGE_INTERVAL_S = 1.0
_MIN_REFRESH_INTERVAL_S = 5.0

VALID_CLIENT_STATES = frozenset({"active", "away"})

# Compare-and-set on the published state. Missing key == offline, so the very first
# "offline" computation never emits a frame. Returns 1 if the state changed.
_PUBLISH_CAS = """
local old = redis.call('GET', KEYS[1])
if not old then old = 'offline' end
if old == ARGV[1] then return 0 end
if ARGV[1] == 'offline' then
  redis.call('DEL', KEYS[1])
else
  redis.call('SET', KEYS[1], ARGV[1], 'EX', tonumber(ARGV[2]))
end
return 1
"""


def _conn_key(session_id: UUID | str) -> str:
    return f"presence:conn:{session_id}"


def _pub_key(session_id: UUID | str) -> str:
    return f"presence:pub:{session_id}"


def _last_key(session_id: UUID | str) -> str:
    return f"presence:last:{session_id}"


def aggregate_state(members: Iterable[str]) -> str:
    """Fold live ``"conn|state"`` members into one state."""
    states = {m.rsplit("|", 1)[-1] for m in members}
    if "active" in states:
        return "active"
    if "away" in states:
        return "away"
    return "offline"


def _to_dt(epoch: float | None) -> datetime | None:
    return datetime.fromtimestamp(epoch, tz=UTC) if epoch else None


def _build_presence(
    members: Iterable[str], redis_last: str | None, db_last: datetime | None
) -> CustomerPresence:
    state = aggregate_state(members)
    if state != "offline":
        return CustomerPresence(state=state, last_seen_at=None)
    candidates = [d for d in (db_last, _to_dt(float(redis_last)) if redis_last else None) if d]
    if candidates:
        aware = [d if d.tzinfo else d.replace(tzinfo=UTC) for d in candidates]
        return CustomerPresence(state="offline", last_seen_at=max(aware))
    return CustomerPresence(state="offline", last_seen_at=None)


def frame(session_id: UUID | str, presence: CustomerPresence) -> dict:
    """The ``customer_presence`` frame sent to agent sockets."""
    return {
        "type": "customer_presence",
        "session_id": str(session_id),
        "state": presence.state,
        "last_seen_at": presence.last_seen_at.isoformat() if presence.last_seen_at else None,
    }


# --------------------------------------------------------------------------- reads (sync)

_sync_client: sync_redis.Redis | None = None


def _get_sync_client() -> sync_redis.Redis:
    global _sync_client
    if _sync_client is None:
        # Short timeouts: a blackholed Redis must degrade presence, not hang list endpoints.
        _sync_client = sync_redis.Redis.from_url(
            settings.REDIS_URL,
            decode_responses=True,
            socket_timeout=_SOCKET_TIMEOUT_S,
            socket_connect_timeout=_SOCKET_TIMEOUT_S,
        )
    return _sync_client


def read_presence_many(
    db: Session,
    session_ids: Iterable[UUID],
    *,
    now: Callable[[], float] = time.time,
) -> dict[UUID, CustomerPresence]:
    """Batched presence for list endpoints: one Redis pipeline (+ one DB query if any
    session is offline). Returns ``{}`` when Redis is unreachable — callers then emit
    ``customer_presence: null`` ("unknown") rather than a wrong state."""
    ids = list(dict.fromkeys(session_ids))
    if not ids:
        return {}
    ts = now()
    try:
        pipe = _get_sync_client().pipeline(transaction=False)
        for sid in ids:
            pipe.zrangebyscore(_conn_key(sid), f"({ts}", "+inf")
            pipe.get(_last_key(sid))
        raw = pipe.execute()
    except sync_redis.RedisError:
        logger.warning("customer presence read failed (Redis unavailable)")
        return {}

    members = {sid: raw[i * 2] for i, sid in enumerate(ids)}
    redis_last = {sid: raw[i * 2 + 1] for i, sid in enumerate(ids)}
    offline = [sid for sid in ids if aggregate_state(members[sid]) == "offline"]

    db_last: dict[UUID, datetime | None] = {}
    if offline:
        from app.db.models.conversation import ConversationSession

        rows = db.execute(
            select(ConversationSession.id, ConversationSession.customer_last_seen_at).where(
                ConversationSession.id.in_(offline)
            )
        ).all()
        db_last = {r[0]: r[1] for r in rows}

    result = {
        sid: _build_presence(members[sid], redis_last[sid], db_last.get(sid)) for sid in offline
    }
    result.update(
        {
            sid: _build_presence(members[sid], redis_last[sid], None)
            for sid in ids
            if sid not in result
        }
    )
    _persist_lazily(db, offline, redis_last, db_last)
    return {sid: result[sid] for sid in ids}


def _persist_lazily(
    db: Session,
    offline: list[UUID],
    redis_last: dict[UUID, str | None],
    db_last: dict[UUID, datetime | None],
) -> None:
    """Offline sessions whose Redis `last` is newer than the DB value (the offline
    transition never ran, e.g. a deploy cancelled it): write it now, best effort."""
    from app.db.models.conversation import ConversationSession

    stale = []
    for sid in offline:
        raw = redis_last.get(sid)
        if not raw or sid not in db_last:
            continue
        seen = _to_dt(float(raw))
        cur = db_last[sid]
        if cur is not None and cur.tzinfo is None:
            cur = cur.replace(tzinfo=UTC)
        if seen and (cur is None or seen > cur):
            stale.append((sid, seen))
    if not stale:
        return
    try:
        for sid, seen in stale:
            db.execute(
                update(ConversationSession)
                .where(ConversationSession.id == sid)
                # Never move it backwards if a newer offline write landed meanwhile.
                .where(
                    or_(
                        ConversationSession.customer_last_seen_at.is_(None),
                        ConversationSession.customer_last_seen_at < seen,
                    )
                )
                .values(customer_last_seen_at=seen)
            )
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("lazy customer_last_seen_at persist failed")


def read_presence(db: Session, session_id: UUID) -> CustomerPresence | None:
    return read_presence_many(db, [session_id]).get(session_id)


# --------------------------------------------------------------------------- writes (async)


class CustomerPresenceTracker:
    """One per process; owns Redis writes, frame publication and offline transitions."""

    def __init__(
        self,
        *,
        now: Callable[[], float] = time.time,
        conn_ttl_s: float = CONN_TTL_S,
        grace_s: float = GRACE_S,
        keepalive_s: float = KEEPALIVE_S,
    ) -> None:
        self._now = now
        self.conn_ttl_s = conn_ttl_s
        self.grace_s = grace_s
        self.keepalive_s = keepalive_s
        self._redis: aioredis.Redis | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._publish_cas: object | None = None
        self._tasks: set[asyncio.Task] = set()

    async def _client(self) -> aioredis.Redis:
        loop = asyncio.get_running_loop()
        if self._redis is None or self._loop is not loop:
            # An asyncio Redis client is bound to its loop; rebuild if the loop changed
            # (test clients spin up a fresh loop per connection).
            self._redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
            self._publish_cas = self._redis.register_script(_PUBLISH_CAS)
            self._loop = loop
        return self._redis

    async def aclose(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks.clear()
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # -- connection entries
    async def set_conn(self, session_id: UUID, conn_id: str, state: str) -> None:
        r = await self._client()
        ts = self._now()
        key = _conn_key(session_id)
        pipe = r.pipeline(transaction=True)
        pipe.zremrangebyscore(key, "-inf", ts)
        pipe.zrem(key, f"{conn_id}|active", f"{conn_id}|away")
        pipe.zadd(key, {f"{conn_id}|{state}": ts + self.conn_ttl_s})
        pipe.expire(key, _KEY_TTL_S)
        pipe.set(_last_key(session_id), repr(ts), ex=_LAST_TTL_S)
        pipe.expire(_pub_key(session_id), _PUB_TTL_S)  # live socket keeps it; lapse clears it
        await pipe.execute()

    async def release_conn(self, session_id: UUID, conn_id: str) -> None:
        """Socket closed: keep the entry for the grace window instead of deleting it."""
        r = await self._client()
        ts = self._now()
        key = _conn_key(session_id)
        pipe = r.pipeline(transaction=True)
        for st in ("active", "away"):
            pipe.zadd(key, {f"{conn_id}|{st}": ts + self.grace_s}, xx=True)
        pipe.expire(key, _KEY_TTL_S)
        pipe.set(_last_key(session_id), repr(ts), ex=_LAST_TTL_S)
        # The published state may not outlive the grace: if the watcher never runs
        # (deploy), a stale value must not swallow the customer's next change.
        pipe.pexpire(_pub_key(session_id), int(self.grace_s * 1000) + _PUB_SLACK_MS)
        await pipe.execute()

    async def current(self, session_id: UUID, db_last: datetime | None = None) -> CustomerPresence:
        r = await self._client()
        pipe = r.pipeline(transaction=False)
        pipe.zrangebyscore(_conn_key(session_id), f"({self._now()}", "+inf")
        pipe.get(_last_key(session_id))
        members, last = await pipe.execute()
        return _build_presence(members, last, db_last)

    # -- publication
    async def recompute_and_publish(
        self,
        session_id: UUID,
        persist_last_seen: Callable[[UUID, datetime], None] | None = None,
    ) -> bool:
        """Derive the state; emit a frame to agents only if it changed. Returns changed."""
        presence = await self.current(session_id)
        r = await self._client()
        changed = bool(
            await self._publish_cas(  # type: ignore[misc]
                keys=[_pub_key(session_id)],
                args=[presence.state, _PUB_TTL_S],
                client=r,
            )
        )
        if not changed:
            return False
        if presence.state == "offline" and persist_last_seen and presence.last_seen_at:
            try:
                await asyncio.to_thread(persist_last_seen, session_id, presence.last_seen_at)
            except Exception:
                logger.exception("failed to persist customer_last_seen_at")
        await connection_manager.fanout_event(
            session_id=session_id,
            message=frame(session_id, presence),
            to_customer=False,
            to_agents=True,
        )
        return True

    def release_and_watch(
        self,
        session_id: UUID,
        conn_id: str,
        persist_last_seen: Callable[[UUID, datetime], None] | None = None,
    ) -> asyncio.Event:
        """Release a closed socket's entry, then re-evaluate once the grace has elapsed.

        Runs as its own task so a handler that is being cancelled (server shutdown,
        client teardown) still releases the entry and still triggers the offline
        transition. The returned event is set once the release itself is done.
        """
        released = asyncio.Event()

        async def _run() -> None:
            try:
                await self.release_conn(session_id, conn_id)
            except Exception:
                logger.exception("customer presence release failed")
            finally:
                released.set()
            await asyncio.sleep(self.grace_s + 0.5)
            try:
                await self.recompute_and_publish(session_id, persist_last_seen)
            except Exception:
                logger.exception("offline presence check failed")

        self._spawn(_run())
        return released

    def open(
        self,
        session_id: UUID,
        persist_last_seen: Callable[[UUID, datetime], None] | None = None,
    ) -> "CustomerPresenceConnection":
        return CustomerPresenceConnection(self, session_id, persist_last_seen)


class CustomerPresenceConnection:
    """Presence handle for one customer socket. Every method is failure-isolated:
    a Redis outage must never break chat, only degrade presence."""

    def __init__(
        self,
        tracker: CustomerPresenceTracker,
        session_id: UUID,
        persist_last_seen: Callable[[UUID, datetime], None] | None,
    ) -> None:
        self._tracker = tracker
        self.session_id = session_id
        self._persist = persist_last_seen
        self.conn_id = uuid.uuid4().hex
        self.state = "active"  # socket open == active until the client says otherwise
        self._last_frame = 0.0  # last accepted client frame (flood guard); keepalive excluded
        self._keepalive: asyncio.Task | None = None
        self._closed = False
        self._pending: str | None = None
        self._trailing: asyncio.Task | None = None

    async def start(self) -> None:
        try:
            await self._tracker.set_conn(self.session_id, self.conn_id, self.state)
            await self._tracker.recompute_and_publish(self.session_id, self._persist)
        except Exception:
            logger.exception("customer presence start failed")
        self._keepalive = self._tracker._spawn(self._keepalive_loop())

    async def _keepalive_loop(self) -> None:
        while True:
            await asyncio.sleep(self._tracker.keepalive_s)
            try:
                await self._tracker.set_conn(self.session_id, self.conn_id, self.state)
            except Exception:
                logger.warning("customer presence keepalive failed")

    async def handle_frame(self, data: dict) -> None:
        """``{"type":"presence","state":"active"|"away"}`` from the customer socket."""
        state = data.get("state")
        if state not in VALID_CLIENT_STATES or self._closed:
            return
        elapsed = self._tracker._now() - self._last_frame
        changed = state != self.state
        window = _MIN_CHANGE_INTERVAL_S if changed else _MIN_REFRESH_INTERVAL_S
        if elapsed < window:
            # Flood guard. A dropped *change* must not be lost (hidden -> visible inside
            # a second would leave agents on "away"): remember the latest and apply it
            # when the window closes. Dropped same-state refreshes are just ignored.
            if changed or self._pending is not None:
                self._pending = state
                self._schedule_trailing(max(0.0, _MIN_CHANGE_INTERVAL_S - elapsed))
            return
        self._pending = None
        await self._apply(state)

    def _schedule_trailing(self, delay: float) -> None:
        if self._trailing is not None and not self._trailing.done():
            return

        async def _run() -> None:
            await asyncio.sleep(delay)
            state, self._pending = self._pending, None
            if state and not self._closed and state != self.state:
                await self._apply(state)

        self._trailing = self._tracker._spawn(_run())

    async def _apply(self, state: str) -> None:
        changed = state != self.state
        self.state = state
        self._last_frame = self._tracker._now()
        try:
            await self._tracker.set_conn(self.session_id, self.conn_id, state)
            if changed:
                await self._tracker.recompute_and_publish(self.session_id, self._persist)
        except Exception:
            logger.exception("customer presence update failed")

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._keepalive:
            self._keepalive.cancel()
        if self._trailing:
            self._trailing.cancel()
        released = self._tracker.release_and_watch(self.session_id, self.conn_id, self._persist)
        await released.wait()


def persist_last_seen_factory(session_factory) -> Callable[[UUID, datetime], None]:
    """Build the sync DB writer for ``customer_last_seen_at`` from a session factory."""

    def _persist(session_id: UUID, seen_at: datetime) -> None:
        from app.db.models.conversation import ConversationSession

        db = session_factory()
        try:
            row = db.get(ConversationSession, session_id)
            if row is not None:
                row.customer_last_seen_at = seen_at
                db.commit()
        finally:
            db.close()

    return _persist


presence_tracker = CustomerPresenceTracker()
