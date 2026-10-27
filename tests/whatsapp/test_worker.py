"""arq wiring: job bodies, per-chat lock, retry mapping, idempotent enqueue (real Redis)."""

from __future__ import annotations

from uuid import uuid4

import pytest
from arq import Retry
from arq.connections import RedisSettings, create_pool

from app.core.config import settings
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


async def test_event_job_retries_when_the_chat_is_busy(db, fake_sidecar, queue, llm, monkeypatch):
    monkeypatch.setattr(jobs, "SessionLocal", lambda: _NoCloseSession(db))
    conn = connect(db, fake_sidecar, make_tenant(db))
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
        assert len(mine) == 1 and mine[0].args[2] == "hello"  # the duplicate was rejected
        evt = {"id": uuid4().hex, "type": "session.status", "session_key": "k", "at": "x"}
        await whatsapp_enqueue.enqueue_event(evt)
        await whatsapp_enqueue.enqueue_event(evt)
        assert len([j for j in await pool.queued_jobs() if j.job_id == f"wa_evt:{evt['id']}"]) == 1
    finally:
        for job_id in (f"wa_send:{key}", f"wa_evt:{locals().get('evt', {}).get('id')}"):
            await pool.delete(f"arq:job:{job_id}")
            await pool.delete(f"arq:result:{job_id}")
            await pool.zrem("arq:queue", job_id)
        await pool.close()
