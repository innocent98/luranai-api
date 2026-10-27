"""arq wiring: job bodies, per-chat lock, retry mapping, idempotent enqueue (real Redis)."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from arq import Retry
from arq.connections import RedisSettings, create_pool

from app.core.config import settings
from app.core.encryption import decrypt_field, encrypt_field
from app.services import whatsapp_enqueue
from app.workers import whatsapp as jobs
from tests.whatsapp.conftest import connect, inbound, make_tenant

pytestmark = pytest.mark.asyncio


class _NoCloseSession:
    def __init__(self, db):
        self._db = db

    def close(self):
        pass

    def __getattr__(self, name):
        return getattr(self._db, name)


class FakeRedis:
    def __init__(self, free: bool = True):
        self.free, self.deleted = free, []

    async def set(self, key, value, ex=None, nx=False):
        return self.free

    async def delete(self, key):
        self.deleted.append(key)


async def test_event_job_runs_pipeline_and_releases_the_chat_lock(
    db, fake_sidecar, queue, llm, monkeypatch
):
    monkeypatch.setattr(jobs, "SessionLocal", lambda: _NoCloseSession(db))
    conn = connect(db, fake_sidecar, make_tenant(db))
    redis = FakeRedis(free=True)
    outcome = await jobs.process_whatsapp_event({"redis": redis}, inbound(conn))
    assert outcome == "ai_reply" and len(queue.sends) == 1
    assert len(redis.deleted) == 1 and redis.deleted[0].startswith("wa:chatlock:")
    assert "lid" not in redis.deleted[0]  # the chat ref is hashed, never a Redis key in clear


async def test_event_job_retries_when_the_chat_is_busy(db, fake_sidecar, queue, llm, monkeypatch):
    monkeypatch.setattr(jobs, "SessionLocal", lambda: _NoCloseSession(db))
    conn = connect(db, fake_sidecar, make_tenant(db))
    monkeypatch.setattr(jobs, "_LOCK_WAIT_TRIES", 3)
    monkeypatch.setattr(jobs, "_LOCK_POLL_S", 0)
    redis = FakeRedis(free=False)
    with pytest.raises(Retry):
        await jobs.process_whatsapp_event({"redis": redis}, inbound(conn))
    assert llm.calls == 0 and redis.deleted == []  # never touched a lock it did not own


async def test_send_job_maps_transient_failures_to_arq_retry(db, fake_sidecar, monkeypatch):
    from app.db.models.whatsapp import WhatsAppChat
    from app.utils.datetime import utcnow_aware

    monkeypatch.setattr(jobs, "SessionLocal", lambda: _NoCloseSession(db))
    conn = connect(db, fake_sidecar, make_tenant(db))
    chat = WhatsAppChat(
        tenant_id=conn.tenant_id,
        connection_id=conn.id,
        contact_hmac="h" * 64,
        contact_ref="r@lid",
        last_inbound_at=utcnow_aware(),
    )
    db.add(chat)
    db.commit()
    assert (
        await jobs.send_whatsapp_message({"job_try": 1}, str(conn.id), str(chat.id), "hi") == "sent"
    )
    fake_sidecar.send_error = (500, "boom")
    with pytest.raises(Retry):
        await jobs.send_whatsapp_message({"job_try": 1}, str(conn.id), str(chat.id), "hi")


async def test_worker_registers_both_jobs_with_their_own_try_budget():
    from app.workers.document_processor import WorkerSettings

    by_name = {
        getattr(f, "name", getattr(f, "__name__", None)): f for f in WorkerSettings.functions
    }
    assert by_name["process_whatsapp_event"].max_tries == 10
    assert by_name["send_whatsapp_message"].max_tries == 5


async def test_enqueue_is_idempotent_per_key_against_real_redis():
    redis_settings = RedisSettings.from_dsn(settings.REDIS_URL)
    redis_settings.conn_retries = 0
    try:
        pool = await create_pool(redis_settings)
    except Exception:  # noqa: BLE001
        pytest.skip("no Redis")
    key = f"test-{uuid4().hex}"
    try:
        await whatsapp_enqueue.enqueue_send(uuid4(), uuid4(), "hello", key)
        await whatsapp_enqueue.enqueue_send(uuid4(), uuid4(), "hello again", key)  # same key
        queued = await pool.queued_jobs()
        mine = [j for j in queued if j.job_id == f"wa_send:{key}"]
        # the duplicate was rejected, and the stored argument is sealed (no plaintext in Redis)
        assert len(mine) == 1 and mine[0].args[2] != "hello"
        assert decrypt_field(mine[0].args[2]) == "hello"
        evt = {"id": uuid4().hex, "type": "session.status", "session_key": "k", "at": "x"}
        await whatsapp_enqueue.enqueue_event(evt)
        await whatsapp_enqueue.enqueue_event(evt)
        queued_evt = [j for j in await pool.queued_jobs() if j.job_id == f"wa_evt:{evt['id']}"]
        assert len(queued_evt) == 1
        assert "session.status" not in str(queued_evt[0].args)  # sealed
        assert json.loads(decrypt_field(queued_evt[0].args[0])) == evt
    finally:
        for job_id in (f"wa_send:{key}", f"wa_evt:{locals().get('evt', {}).get('id')}"):
            await pool.delete(f"arq:job:{job_id}")
            await pool.delete(f"arq:result:{job_id}")
            await pool.zrem("arq:queue", job_id)
        await pool.close()


async def test_event_job_accepts_the_sealed_payload_the_enqueuer_produces(
    db, fake_sidecar, queue, llm, monkeypatch
):
    monkeypatch.setattr(jobs, "SessionLocal", lambda: _NoCloseSession(db))
    conn = connect(db, fake_sidecar, make_tenant(db))
    sealed = encrypt_field(json.dumps(inbound(conn)))
    assert await jobs.process_whatsapp_event({}, sealed) == "ai_reply"


async def test_identifier_limiter_blocks_after_max_calls_against_real_redis():
    from fastapi import HTTPException

    from app.utils.http_rate_limiter import enforce_identifier_limit
    from app.utils.rate_limiter import RedisRateLimiter

    limiter = RedisRateLimiter(
        key_prefix=f"http_wa_test_{uuid4().hex[:8]}", max_calls=2, window_seconds=60
    )
    try:
        await enforce_identifier_limit(limiter, "tenant:x")
        await enforce_identifier_limit(limiter, "tenant:x")
        await enforce_identifier_limit(limiter, "tenant:y")  # other identifier unaffected
        with pytest.raises(HTTPException) as exc:
            await enforce_identifier_limit(limiter, "tenant:x")
        assert exc.value.status_code == 429
        assert exc.value.headers["Retry-After"] == "60"
    except OSError:
        pytest.skip("no Redis")
    finally:
        await limiter.reset("tenant:x")
        await limiter.reset("tenant:y")
        await limiter.close()


async def test_event_job_waits_for_a_busy_chat_instead_of_burning_retries(
    db, fake_sidecar, queue, llm, monkeypatch
):
    monkeypatch.setattr(jobs, "SessionLocal", lambda: _NoCloseSession(db))
    monkeypatch.setattr(jobs, "_LOCK_POLL_S", 0)
    conn = connect(db, fake_sidecar, make_tenant(db))

    class BusyThenFree(FakeRedis):
        def __init__(self):
            super().__init__(free=False)
            self.calls = 0

        async def set(self, key, value, ex=None, nx=False):
            self.calls += 1
            return self.calls >= 4  # the previous turn finishes after three polls

    redis = BusyThenFree()
    assert await jobs.process_whatsapp_event({"redis": redis}, inbound(conn)) == "ai_reply"
    assert redis.calls == 4 and len(redis.deleted) == 1
