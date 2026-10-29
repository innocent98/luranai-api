"""Capacity alert: thresholds, Redis dedupe, recipients, failure isolation, stable tenant 503."""

from __future__ import annotations

import threading
import time
from uuid import uuid4

import httpx
import pytest
import redis.asyncio as aioredis

from app.channels.base import SidecarHealth, SidecarUnavailable
from app.channels.linked_device import LinkedDeviceAdapter
from app.core.config import settings
from app.services import whatsapp_capacity as cap
from app.services.email_service import EmailService
from app.workers import whatsapp as jobs
from tests.whatsapp.conftest import (
    PHONE_A,
    PHONE_B,
    SIDECAR_TOKEN,
    connect,
    consent_body,
    login_admin,
    make_tenant,
)

REAL_SCHEDULE = cap.schedule_post_create_check  # captured at import, before the autouse stub
TENANT_503 = {
    "code": "capacity_exceeded",
    "message": "WhatsApp is temporarily at capacity. Please try again later.",
}


def health(rss: float = 100, refuse: float = 400) -> SidecarHealth:
    return SidecarHealth(sessions=1, max_sessions=3, rss_mb=rss, rss_refuse_mb=refuse)


@pytest.fixture
async def redis_ns(monkeypatch):
    """A unique key namespace per test on the real Redis, cleaned up afterwards."""
    prefix = f"wa:capacity_alert:test:{uuid4().hex}:"
    monkeypatch.setattr(cap, "KEY_PREFIX", prefix)
    client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    yield client, prefix
    for key in [k async for k in client.scan_iter(match=f"{prefix}*")]:
        await client.delete(key)
    await client.aclose()


@pytest.fixture
def sent(monkeypatch):
    """Stand-in for the SMTP send: records (to, condition, lines); `fail` makes it return False."""

    class Sent:
        def __init__(self) -> None:
            self.mails: list[tuple[str, str, list[str]]] = []
            self.fail = False
            self.raises = False

    box = Sent()

    def fake(self, to_email, condition, detail_lines):
        if box.raises:
            raise RuntimeError("smtp down")
        box.mails.append((to_email, condition, detail_lines))
        return not box.fail

    monkeypatch.setattr(EmailService, "send_whatsapp_capacity_alert_email", fake)
    monkeypatch.setattr(settings, "PLATFORM_ALERT_EMAILS", "ops@example.test")
    monkeypatch.setattr(settings, "WHATSAPP_MAX_CONNECTIONS", 5)
    monkeypatch.setattr(settings, "WHATSAPP_CAPACITY_ALERT_PERCENT", 80)
    return box


def seed_live(db, fake, n: int) -> None:
    for i in range(n):
        connect(db, fake, make_tenant(db), phone=f"23480000{i:05d}")


# ------------------------------------------------------------------ thresholds (pure)
@pytest.mark.parametrize(
    ("live", "fires"), [(0, False), (3, False), (4, True), (5, True), (6, True)]
)
def test_connections_threshold(monkeypatch, live, fires):
    monkeypatch.setattr(settings, "WHATSAPP_MAX_CONNECTIONS", 5)
    monkeypatch.setattr(settings, "WHATSAPP_CAPACITY_ALERT_PERCENT", 80)
    got = [a.condition for a in cap.evaluate(live, None)]
    assert got == (["connections"] if fires else [])


@pytest.mark.parametrize(
    ("limit", "threshold"), [(1, 1), (2, 1), (3, 2), (4, 3), (5, 4), (10, 8), (100, 80)]
)
def test_connections_threshold_is_the_percent_or_one_short_of_the_limit(
    monkeypatch, limit, threshold
):
    """At small limits the percent would only fire at the 503 point, so alert one short of it."""
    monkeypatch.setattr(settings, "WHATSAPP_MAX_CONNECTIONS", limit)
    monkeypatch.setattr(settings, "WHATSAPP_CAPACITY_ALERT_PERCENT", 80)
    assert [a.condition for a in cap.evaluate(threshold - 1, None)] == []
    assert [a.condition for a in cap.evaluate(threshold, None)] == ["connections"]
    assert [a.condition for a in cap.evaluate(limit, None)] == ["connections"]


def test_connections_alert_wording(monkeypatch):
    monkeypatch.setattr(settings, "WHATSAPP_MAX_CONNECTIONS", 3)
    monkeypatch.setattr(settings, "WHATSAPP_CAPACITY_ALERT_PERCENT", 80)
    (alert,) = cap.evaluate(2, None)
    assert alert.detail_lines[0].startswith("Live WhatsApp connections: 2 of 3 allowed")
    assert alert.detail_lines[1] == "Alert threshold: 80% or one connection short of the limit."


@pytest.mark.parametrize(("rss", "fires"), [(100, False), (319, False), (320, True), (400, True)])
def test_memory_threshold(monkeypatch, rss, fires):
    monkeypatch.setattr(settings, "WHATSAPP_MAX_CONNECTIONS", 100)
    got = [a.condition for a in cap.evaluate(0, health(rss=rss, refuse=400))]
    assert got == (["sidecar_memory"] if fires else [])


def test_conditions_are_independent_and_zero_limits_never_fire(monkeypatch):
    monkeypatch.setattr(settings, "WHATSAPP_MAX_CONNECTIONS", 5)
    both = cap.evaluate(4, health(rss=350))
    assert sorted(a.condition for a in both) == sorted(cap.CONDITIONS)
    assert [a.condition for a in cap.evaluate(4, health(rss=10))] == ["connections"]
    assert [a.condition for a in cap.evaluate(0, health(rss=350))] == ["sidecar_memory"]
    monkeypatch.setattr(settings, "WHATSAPP_MAX_CONNECTIONS", 0)
    assert cap.evaluate(0, health(rss=0, refuse=0)) == []
    assert cap.evaluate(9, None) == []


def test_alert_details_are_counts_only(monkeypatch):
    monkeypatch.setattr(settings, "WHATSAPP_MAX_CONNECTIONS", 5)
    text = " ".join(line for a in cap.evaluate(4, health(rss=350)) for line in a.detail_lines)
    assert "4 of 5" in text and "350 MB" in text


# ------------------------------------------------------------------ check_and_alert
async def test_fires_emails_once_then_dedupes(db, fake_sidecar, redis_ns, sent):
    client, prefix = redis_ns
    seed_live(db, fake_sidecar, 4)
    assert await cap.check_and_alert(db, include_health=False) == ["connections"]
    assert len(sent.mails) == 1 and sent.mails[0][:2] == ("ops@example.test", "connections")
    ttl = await client.ttl(f"{prefix}connections")
    assert 21000 < ttl <= 21600
    assert await cap.check_and_alert(db, include_health=False) == []
    assert len(sent.mails) == 1  # second call inside the window sends nothing


async def test_a_different_condition_still_sends(db, fake_sidecar, redis_ns, sent, monkeypatch):
    seed_live(db, fake_sidecar, 4)
    assert await cap.check_and_alert(db, include_health=False) == ["connections"]
    # the fake sidecar reports 120/400 MB: below the threshold, so only the held key matters
    assert await cap.check_and_alert(db, include_health=True) == []

    async def hot(self):
        return health(rss=390)

    monkeypatch.setattr(LinkedDeviceAdapter, "get_health", hot)
    assert await cap.check_and_alert(db, include_health=True) == ["sidecar_memory"]
    assert [m[1] for m in sent.mails] == ["connections", "sidecar_memory"]


async def test_health_is_only_fetched_when_asked(db, fake_sidecar, redis_ns, sent, monkeypatch):
    calls: list[int] = []

    async def hot(self):
        calls.append(1)
        return health(rss=399)

    monkeypatch.setattr(LinkedDeviceAdapter, "get_health", hot)
    assert await cap.check_and_alert(db, include_health=False) == []
    assert calls == []
    assert await cap.check_and_alert(db, include_health=True) == ["sidecar_memory"]
    assert calls == [1]


async def test_unreachable_sidecar_still_checks_connections(
    db, fake_sidecar, redis_ns, sent, monkeypatch
):
    monkeypatch.setattr(settings, "WHATSAPP_SIDECAR_URL", "")
    monkeypatch.setattr(settings, "WHATSAPP_MAX_CONNECTIONS", 1)
    connect(db, fake_sidecar, make_tenant(db))
    assert await cap.check_and_alert(db, include_health=True) == ["connections"]


@pytest.mark.parametrize("blank", ["", "   ", " , ,"])
async def test_no_recipients_sends_nothing_and_keeps_the_key_free(
    db, fake_sidecar, redis_ns, sent, monkeypatch, blank
):
    client, prefix = redis_ns
    monkeypatch.setattr(settings, "PLATFORM_ALERT_EMAILS", blank)
    seed_live(db, fake_sidecar, 4)
    assert await cap.check_and_alert(db, include_health=False) == []
    assert sent.mails == []
    assert await client.exists(f"{prefix}connections") == 0
    # fixing the config makes the very next run send
    monkeypatch.setattr(settings, "PLATFORM_ALERT_EMAILS", "ops@example.test")
    assert await cap.check_and_alert(db, include_health=False) == ["connections"]


async def test_each_recipient_gets_one_email_trimmed(db, fake_sidecar, redis_ns, sent, monkeypatch):
    monkeypatch.setattr(
        settings, "PLATFORM_ALERT_EMAILS", " a@example.test , ,b@example.test,  c@example.test "
    )
    seed_live(db, fake_sidecar, 4)
    assert await cap.check_and_alert(db, include_health=False) == ["connections"]
    assert [m[0] for m in sent.mails] == ["a@example.test", "b@example.test", "c@example.test"]


@pytest.mark.parametrize("mode", ["returns_false", "raises"])
async def test_smtp_failure_never_raises_and_releases_the_key(
    db, fake_sidecar, redis_ns, sent, mode
):
    client, prefix = redis_ns
    seed_live(db, fake_sidecar, 4)
    sent.fail, sent.raises = mode == "returns_false", mode == "raises"
    assert await cap.check_and_alert(db, include_health=False) == []
    assert await client.exists(f"{prefix}connections") == 0
    sent.fail = sent.raises = False  # SMTP recovers: the next run retries
    assert await cap.check_and_alert(db, include_health=False) == ["connections"]


async def test_partial_delivery_counts_as_sent(db, fake_sidecar, redis_ns, sent, monkeypatch):
    client, prefix = redis_ns
    monkeypatch.setattr(settings, "PLATFORM_ALERT_EMAILS", "a@example.test,b@example.test")
    results = iter([False, True])

    def flaky(self, to_email, condition, detail_lines):
        sent.mails.append((to_email, condition, detail_lines))
        return next(results)

    monkeypatch.setattr(EmailService, "send_whatsapp_capacity_alert_email", flaky)
    seed_live(db, fake_sidecar, 4)
    assert await cap.check_and_alert(db, include_health=False) == ["connections"]
    assert await client.exists(f"{prefix}connections") == 1


async def test_redis_down_sends_nothing_and_does_not_raise(
    db, fake_sidecar, sent, monkeypatch, log_lines
):
    monkeypatch.setattr(settings, "REDIS_URL", "redis://127.0.0.1:1/0")
    seed_live(db, fake_sidecar, 4)
    assert await cap.check_and_alert(db, include_health=False) == []
    assert sent.mails == []
    assert any("ConnectionError" in entry for entry in log_lines)


async def test_unexpected_failure_is_swallowed(db, sent, monkeypatch):
    def boom(_db):
        raise RuntimeError("db gone")

    monkeypatch.setattr(cap, "_live_count", boom)
    assert await cap.check_and_alert(db, include_health=True) == []


# ------------------------------------------------------------------ post-create hook
async def test_schedule_runs_a_count_only_check_in_its_own_session(monkeypatch):
    seen: dict = {}

    class FakeSession:
        closed = False

        def close(self):
            self.closed = True

    session = FakeSession()

    async def fake_check(db, *, include_health):
        seen.update(db=db, include_health=include_health)
        return []

    monkeypatch.setattr(cap, "SessionLocal", lambda: session)
    monkeypatch.setattr(cap, "check_and_alert", fake_check)
    REAL_SCHEDULE()
    for task in list(cap._tasks):
        await task
    assert seen == {"db": session, "include_health": False}
    assert session.closed and not cap._tasks


async def test_schedule_never_raises_even_if_the_session_cannot_open(monkeypatch):
    def broken():
        raise RuntimeError("no db")

    monkeypatch.setattr(cap, "SessionLocal", broken)
    REAL_SCHEDULE()
    for task in list(cap._tasks):
        await task  # the task itself swallows the error


def test_schedule_without_a_running_loop_does_not_raise():
    REAL_SCHEDULE()


# ------------------------------------------------------------------ adapter /health
async def test_get_health_reads_the_fake_sidecar(fake_sidecar):
    h = await LinkedDeviceAdapter(transport=httpx.ASGITransport(app=fake_sidecar.app)).get_health()
    assert h == SidecarHealth(sessions=0, max_sessions=3, rss_mb=120, rss_refuse_mb=400)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json={"sessions": 1}),
        httpx.Response(
            200, json={"sessions": "x", "max_sessions": 3, "rss_mb": 1, "rss_refuse_mb": 2}
        ),
        httpx.Response(
            200, json={"sessions": True, "max_sessions": 3, "rss_mb": 1, "rss_refuse_mb": 2}
        ),
        httpx.Response(200, json=[1, 2]),
        httpx.Response(200, content=b"not json"),
        httpx.Response(500, json={}),
        httpx.Response(401, json={}),
    ],
)
async def test_get_health_malformed_or_failing_is_sidecar_unavailable(wa_settings, response):
    adapter = LinkedDeviceAdapter(transport=httpx.MockTransport(lambda request: response))
    with pytest.raises(SidecarUnavailable):
        await adapter.get_health()


async def test_get_health_sends_the_bearer_token(wa_settings):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["authorization"])
        return httpx.Response(
            200, json={"sessions": 0, "max_sessions": 1, "rss_mb": 1, "rss_refuse_mb": 2}
        )

    await LinkedDeviceAdapter(transport=httpx.MockTransport(handler)).get_health()
    assert seen == [f"Bearer {SIDECAR_TOKEN}"]


# ------------------------------------------------------------------ cron
async def test_cron_does_nothing_when_whatsapp_is_disabled(monkeypatch):
    called: list[int] = []

    async def spy(db, *, include_health):
        called.append(1)
        return []

    monkeypatch.setattr(cap, "check_and_alert", spy)
    monkeypatch.setattr(settings, "WHATSAPP_ENABLED", False)
    assert await jobs.whatsapp_capacity_check({}) == {"skipped": True, "alerts": []}
    assert called == []


async def test_cron_skips_when_the_sidecar_is_not_configured(wa_settings, monkeypatch):
    monkeypatch.setattr(settings, "WHATSAPP_SIDECAR_URL", "")
    assert (await jobs.whatsapp_capacity_check({}))["skipped"] is True


async def test_cron_runs_with_health_and_never_raises(wa_settings, monkeypatch):
    seen: list[bool] = []

    class FakeSession:
        def close(self):
            pass

    async def spy(db, *, include_health):
        seen.append(include_health)
        return ["connections"]

    monkeypatch.setattr(jobs, "SessionLocal", lambda: FakeSession())
    monkeypatch.setattr(cap, "check_and_alert", spy)
    assert await jobs.whatsapp_capacity_check({}) == {"skipped": False, "alerts": ["connections"]}
    assert seen == [True]

    def broken():
        raise RuntimeError("db down")

    monkeypatch.setattr(jobs, "SessionLocal", broken)
    out = await jobs.whatsapp_capacity_check({})
    assert out["alerts"] == [] and out["error"] == "RuntimeError"


def test_cron_is_registered_every_five_minutes():
    from app.workers.document_processor import WorkerSettings

    names = {getattr(f, "name", getattr(f, "__name__", "")) for f in WorkerSettings.functions}
    assert "whatsapp_capacity_check" in names
    job = next(j for j in WorkerSettings.cron_jobs if j.name.endswith("whatsapp_capacity_check"))
    assert job.minute == set(range(0, 60, 5)) and job.run_at_startup is False


# ------------------------------------------------------------------ create path + stable 503
def test_create_hook_is_called_after_a_successful_create(
    client, db, wa_settings, fake_sidecar, capacity_hook_calls
):
    login_admin(make_tenant(db))
    assert client.post("/api/v1/whatsapp/connections", json=consent_body()).status_code == 201
    assert capacity_hook_calls == [1]


def test_hook_is_not_called_when_create_is_refused(
    client, db, wa_settings, fake_sidecar, capacity_hook_calls, monkeypatch
):
    monkeypatch.setattr(wa_settings, "WHATSAPP_MAX_CONNECTIONS", 1)
    login_admin(make_tenant(db, max_numbers=3))
    assert (
        client.post("/api/v1/whatsapp/connections", json=consent_body(PHONE_A)).status_code == 201
    )
    r = client.post("/api/v1/whatsapp/connections", json=consent_body(PHONE_B))
    assert r.status_code == 503 and capacity_hook_calls == [1]


def test_create_still_returns_201_when_the_alert_machinery_raises(
    client, db, wa_settings, fake_sidecar, monkeypatch
):
    monkeypatch.setattr(cap, "schedule_post_create_check", REAL_SCHEDULE)
    ran = threading.Event()

    class FakeSession:
        def close(self):
            pass

    async def boom(db, *, include_health):
        ran.set()
        raise RuntimeError("alert machinery down")

    monkeypatch.setattr(cap, "SessionLocal", lambda: FakeSession())
    monkeypatch.setattr(cap, "check_and_alert", boom)
    login_admin(make_tenant(db))
    r = client.post("/api/v1/whatsapp/connections", json=consent_body())
    assert r.status_code == 201 and r.json()["status"] == "awaiting_pair"
    assert ran.wait(5)  # the background check really ran (and failed) without touching the response


def test_create_still_returns_201_when_smtp_and_redis_are_down(
    client, db, wa_settings, fake_sidecar, monkeypatch
):
    monkeypatch.setattr(cap, "schedule_post_create_check", REAL_SCHEDULE)
    monkeypatch.setattr(wa_settings, "PLATFORM_ALERT_EMAILS", "ops@example.test")
    monkeypatch.setattr(wa_settings, "REDIS_URL", "redis://127.0.0.1:1/0")
    ran = threading.Event()
    results: list[list[str]] = []
    real_check = cap.check_and_alert

    class FakeSession:
        def close(self):
            pass

    async def recording_check(db, *, include_health):
        ran.set()
        results.append(await real_check(db, include_health=include_health))
        return results[-1]

    def smtp_down(self, *a, **k):
        raise RuntimeError("smtp down")

    # The check must be reached and must see the platform at its limit, so the Redis and SMTP
    # failure paths really run; the real SessionLocal is not the test database.
    monkeypatch.setattr(cap, "SessionLocal", lambda: FakeSession())
    monkeypatch.setattr(cap, "_live_count", lambda _db: wa_settings.WHATSAPP_MAX_CONNECTIONS)
    monkeypatch.setattr(cap, "check_and_alert", recording_check)
    monkeypatch.setattr(EmailService, "send_whatsapp_capacity_alert_email", smtp_down)
    login_admin(make_tenant(db))
    r = client.post("/api/v1/whatsapp/connections", json=consent_body())
    assert r.status_code == 201
    assert ran.wait(5)  # the alert machinery really ran (and failed) behind the response
    deadline = time.monotonic() + 5
    while not results and time.monotonic() < deadline:
        time.sleep(0.05)
    assert results == [[]]  # Redis unreachable: nothing sent, nothing raised


def test_backend_guard_503_body_is_the_stable_message(
    client, db, wa_settings, fake_sidecar, monkeypatch
):
    monkeypatch.setattr(wa_settings, "WHATSAPP_MAX_CONNECTIONS", 1)
    login_admin(make_tenant(db, max_numbers=3))
    assert (
        client.post("/api/v1/whatsapp/connections", json=consent_body(PHONE_A)).status_code == 201
    )
    r = client.post("/api/v1/whatsapp/connections", json=consent_body(PHONE_B))
    assert r.status_code == 503 and r.json() == TENANT_503


def test_sidecar_side_capacity_503_body_is_the_same(client, db, wa_settings, fake_sidecar):
    fake_sidecar.max_sessions = 0
    login_admin(make_tenant(db))
    r = client.post("/api/v1/whatsapp/connections", json=consent_body())
    assert r.status_code == 503 and r.json() == TENANT_503


def test_repair_of_a_logged_out_connection_503_uses_the_same_message(
    client, db, wa_settings, fake_sidecar, monkeypatch
):
    monkeypatch.setattr(wa_settings, "WHATSAPP_MAX_CONNECTIONS", 1)
    tenant = make_tenant(db, max_numbers=3)
    login_admin(tenant)
    connect(db, fake_sidecar, tenant, PHONE_A, status="connected")
    out = connect(db, fake_sidecar, tenant, PHONE_B, status="logged_out")
    r = client.post(f"/api/v1/whatsapp/connections/{out.id}/pairing-code")
    assert r.status_code == 503 and r.json() == TENANT_503


def test_capacity_message_constant_is_used_everywhere():
    import inspect

    from app.services import whatsapp_service

    source = inspect.getsource(whatsapp_service)
    assert source.count("CAPACITY_MESSAGE") == 5  # definition + four raise sites
    assert "at capacity right now" not in source
    assert whatsapp_service.CAPACITY_MESSAGE == TENANT_503["message"]
