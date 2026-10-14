"""Tests for the CRM webhook ARQ job (Stage 3 Slice 5e Task 3)."""

import json

import pytest
import httpx
from uuid import uuid4
from unittest.mock import patch, MagicMock

from app.db.models.tenant import Tenant
from app.db.models.lead import Lead
from app.db.models.sales_agent_config import TenantSalesAgentConfig
from app.db.models.notification_log import NotificationLog, NotifEvent, NotifChannel, NotifStatus
from app.workers.sales_notifications import sync_lead_to_crm


class _NoCloseSession:
    """Proxy that swallows close() so the worker's finally block does not close the test session."""

    def __init__(self, session):
        self._session = session

    def close(self):
        pass

    def __getattr__(self, name):
        return getattr(self._session, name)


def _seed(db, *, url="https://hooks.zapier.com/hooks/catch/1/x"):
    t = Tenant(
        name=f"t_{uuid4().hex[:8]}",
        display_name="Acme",
        api_key_hash=uuid4().hex,
        subscription_tier="trial",
        subscription_status="trialing",
    )
    db.add(t)
    db.flush()
    cfg = TenantSalesAgentConfig.default_for(t.id)
    cfg.crm_webhook_url = url
    db.add(cfg)
    lead = Lead(
        tenant_id=t.id,
        conversation_id=uuid4(),
        name="Dana",
        email="d@co.com",
        company="Acme",
        team_size="11-50",
        industry="SaaS",
        intent="DEMO_REQUEST",
        lead_score=90,
        source="chat",
    )
    db.add(lead)
    db.flush()
    log = NotificationLog(
        tenant_id=t.id,
        event=NotifEvent.SALES_READY_LEAD,
        channel=NotifChannel.CRM,
        entity_type="lead",
        entity_id=lead.id,
        recipient="crm",
        status=NotifStatus.PENDING,
        title="New sales-ready lead",
        body="Dana · Acme · score 90",
    )
    db.add(log)
    db.commit()
    return log.id, lead.id


def _resp(code, json_body=None):
    if json_body is None:
        return httpx.Response(code)
    return httpx.Response(code, json=json_body)


async def test_crm_2xx_marks_sent_and_posts_lead_payload(db_session, monkeypatch):
    log_id, lead_id = _seed(db_session)
    import app.workers.sales_notifications as notif_mod

    monkeypatch.setattr(notif_mod, "SessionLocal", lambda: _NoCloseSession(db_session))

    with patch("httpx.AsyncClient.send", return_value=_resp(200)) as post:
        await sync_lead_to_crm({}, str(log_id))

    post.assert_called_once()
    sent = post.call_args[0][0]
    payload = json.loads(sent.content)
    assert sent.headers["host"] == "hooks.zapier.com"
    assert sent.url.path == "/hooks/catch/1/x"
    assert payload["email"] == "d@co.com"
    assert payload["company"] == "Acme"
    assert payload["lead_score"] == 90
    assert payload["lead_id"] == str(lead_id)
    assert db_session.get(NotificationLog, log_id).status == NotifStatus.SENT


async def test_crm_2xx_with_id_writes_back_crm_id(db_session, monkeypatch):
    log_id, lead_id = _seed(db_session)
    import app.workers.sales_notifications as notif_mod

    monkeypatch.setattr(notif_mod, "SessionLocal", lambda: _NoCloseSession(db_session))

    with patch("httpx.AsyncClient.send", return_value=_resp(200, {"id": "crm-789"})):
        await sync_lead_to_crm({}, str(log_id))

    assert db_session.get(NotificationLog, log_id).status == NotifStatus.SENT
    assert db_session.get(Lead, lead_id).crm_id == "crm-789"


async def test_crm_2xx_without_id_leaves_crm_id_none(db_session, monkeypatch):
    log_id, lead_id = _seed(db_session)
    import app.workers.sales_notifications as notif_mod

    monkeypatch.setattr(notif_mod, "SessionLocal", lambda: _NoCloseSession(db_session))

    with patch("httpx.AsyncClient.send", return_value=_resp(200, {"ok": True})):
        await sync_lead_to_crm({}, str(log_id))

    assert db_session.get(NotificationLog, log_id).status == NotifStatus.SENT
    assert db_session.get(Lead, lead_id).crm_id is None


async def test_crm_5xx_raises_for_retry(db_session, monkeypatch):
    log_id, _ = _seed(db_session)
    import app.workers.sales_notifications as notif_mod

    monkeypatch.setattr(notif_mod, "SessionLocal", lambda: _NoCloseSession(db_session))

    with patch("httpx.AsyncClient.send", return_value=_resp(503)):
        with pytest.raises(Exception):
            await sync_lead_to_crm({}, str(log_id))

    assert db_session.get(NotificationLog, log_id).status == NotifStatus.FAILED


async def test_crm_4xx_no_retry(db_session, monkeypatch):
    log_id, _ = _seed(db_session)
    import app.workers.sales_notifications as notif_mod

    monkeypatch.setattr(notif_mod, "SessionLocal", lambda: _NoCloseSession(db_session))

    with patch("httpx.AsyncClient.send", return_value=_resp(404)):
        await sync_lead_to_crm({}, str(log_id))  # must NOT raise

    assert db_session.get(NotificationLog, log_id).status == NotifStatus.FAILED


async def test_crm_missing_url_marks_failed(db_session, monkeypatch):
    log_id, _ = _seed(db_session, url=None)
    import app.workers.sales_notifications as notif_mod

    monkeypatch.setattr(notif_mod, "SessionLocal", lambda: _NoCloseSession(db_session))

    with patch("httpx.AsyncClient.send") as post:
        await sync_lead_to_crm({}, str(log_id))

    post.assert_not_called()
    assert db_session.get(NotificationLog, log_id).status == NotifStatus.FAILED


async def test_crm_already_sent_is_noop(db_session, monkeypatch):
    log_id, _ = _seed(db_session)
    db_session.get(NotificationLog, log_id).status = NotifStatus.SENT
    db_session.commit()
    import app.workers.sales_notifications as notif_mod

    monkeypatch.setattr(notif_mod, "SessionLocal", lambda: _NoCloseSession(db_session))

    with patch("httpx.AsyncClient.send") as post:
        await sync_lead_to_crm({}, str(log_id))

    post.assert_not_called()
    assert db_session.get(NotificationLog, log_id).status == NotifStatus.SENT


async def test_crm_blocked_host_marks_failed_no_post(db_session, monkeypatch):
    log_id, _ = _seed(db_session, url="http://169.254.169.254/latest/meta-data/")
    import app.workers.sales_notifications as notif_mod

    monkeypatch.setattr(notif_mod, "SessionLocal", lambda: _NoCloseSession(db_session))
    with patch("httpx.AsyncClient.send") as post:
        await sync_lead_to_crm({}, str(log_id))  # must NOT raise
    post.assert_not_called()
    log = db_session.get(NotificationLog, log_id)
    assert log.status == NotifStatus.FAILED
    assert log.error == "blocked webhook host"


async def test_crm_network_timeout_raises_and_marks_failed(db_session, monkeypatch):
    """Network/timeout branch: raises for ARQ retry and marks FAILED."""
    log_id, _ = _seed(db_session)
    import app.workers.sales_notifications as notif_mod

    monkeypatch.setattr(notif_mod, "SessionLocal", lambda: _NoCloseSession(db_session))

    with patch("httpx.AsyncClient.send", side_effect=httpx.TimeoutException("timeout")):
        with pytest.raises(Exception):
            await sync_lead_to_crm({}, str(log_id))

    assert db_session.get(NotificationLog, log_id).status == NotifStatus.FAILED


async def test_crm_2xx_contact_id_key_writes_back(db_session, monkeypatch):
    """Non-`id` key: `contactId` in the 2xx body is written back to lead.crm_id."""
    log_id, lead_id = _seed(db_session)
    import app.workers.sales_notifications as notif_mod

    monkeypatch.setattr(notif_mod, "SessionLocal", lambda: _NoCloseSession(db_session))

    with patch("httpx.AsyncClient.send", return_value=_resp(200, {"contactId": "hub-42"})):
        await sync_lead_to_crm({}, str(log_id))

    assert db_session.get(NotificationLog, log_id).status == NotifStatus.SENT
    assert db_session.get(Lead, lead_id).crm_id == "hub-42"


async def test_crm_missing_log_is_noop(db_session, monkeypatch):
    """log is None early-return: no HTTP call and no exception."""
    import app.workers.sales_notifications as notif_mod

    monkeypatch.setattr(notif_mod, "SessionLocal", lambda: _NoCloseSession(db_session))

    with patch("httpx.AsyncClient.send") as post:
        await sync_lead_to_crm({}, str(uuid4()))  # no log seeded

    post.assert_not_called()


async def test_crm_2xx_zero_integer_id_writes_back(db_session, monkeypatch):
    """Regression for id=0: truthy check would skip 0; is-not-None must write '0'."""
    log_id, lead_id = _seed(db_session)
    import app.workers.sales_notifications as notif_mod

    monkeypatch.setattr(notif_mod, "SessionLocal", lambda: _NoCloseSession(db_session))

    with patch("httpx.AsyncClient.send", return_value=_resp(200, {"id": 0})):
        await sync_lead_to_crm({}, str(log_id))

    assert db_session.get(NotificationLog, log_id).status == NotifStatus.SENT
    assert db_session.get(Lead, lead_id).crm_id == "0"


async def test_crm_never_logs_url_or_pii(db_session, monkeypatch, caplog):
    """Spec §6 acceptance: the webhook URL and the PII payload must never appear in log output.

    Drives both a logging error path (network failure → _EmailSendFailed) and the
    SENT path with a token-bearing URL, then asserts the secret token and the lead
    PII (email/name) are absent from everything captured by the logging framework.
    """
    import logging

    # Same publicly-resolving host the other tests use (passes the SSRF guard),
    # with the secret embedded in the path so we can assert it never reaches a log.
    secret_url = "https://hooks.zapier.com/hooks/catch/1/SECRET-TOKEN-zzz999"
    import app.workers.sales_notifications as notif_mod

    monkeypatch.setattr(notif_mod, "SessionLocal", lambda: _NoCloseSession(db_session))

    # Error path: network failure sets log.error + raises (worker may log).
    log_id, _ = _seed(db_session, url=secret_url)
    with caplog.at_level(logging.DEBUG):
        with patch("httpx.AsyncClient.send", side_effect=httpx.ConnectError("boom")):
            with pytest.raises(Exception):
                await sync_lead_to_crm({}, str(log_id))

        # SENT path: a successful POST with the same secret URL.
        log_id2, _ = _seed(db_session, url=secret_url)
        with patch("httpx.AsyncClient.send", return_value=_resp(200)):
            await sync_lead_to_crm({}, str(log_id2))

    assert "SECRET-TOKEN-zzz999" not in caplog.text
    assert "d@co.com" not in caplog.text  # lead email PII
    assert "Dana" not in caplog.text  # lead name PII
    # DB error field stays a generic, URL-free string.
    assert db_session.get(NotificationLog, log_id).error == "crm network error: ConnectError"


async def test_crm_rebinding_host_connects_only_to_validated_ip(db_session, monkeypatch):
    import socket

    log_id, _ = _seed(db_session, url="https://rebind.example.com/hook")
    import app.workers.sales_notifications as notif_mod

    monkeypatch.setattr(notif_mod, "SessionLocal", lambda: _NoCloseSession(db_session))
    seen: list[str] = []

    def fake(host, *a, **k):
        seen.append(host)
        ip = "93.184.216.34" if len(seen) == 1 else "169.254.169.254"
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, 0))]

    monkeypatch.setattr("app.utils.network_security.socket.getaddrinfo", fake)

    with patch("httpx.AsyncClient.send", return_value=_resp(200)) as send:
        await sync_lead_to_crm({}, str(log_id))

    sent = send.call_args[0][0]
    assert sent.url.host == "93.184.216.34"
    assert sent.headers["host"] == "rebind.example.com"
    assert sent.extensions["sni_hostname"] == "rebind.example.com"
    assert seen == ["rebind.example.com"]  # one lookup; httpx never re-resolves the name
    assert db_session.get(NotificationLog, log_id).status == NotifStatus.SENT


async def test_crm_redirect_is_not_followed(db_session, monkeypatch):
    log_id, _ = _seed(db_session)
    import app.workers.sales_notifications as notif_mod

    monkeypatch.setattr(notif_mod, "SessionLocal", lambda: _NoCloseSession(db_session))
    redirect = httpx.Response(302, headers={"location": "http://169.254.169.254/latest/"})
    with patch("httpx.AsyncClient.send", return_value=redirect) as send:
        await sync_lead_to_crm({}, str(log_id))
    assert send.call_count == 1
    assert db_session.get(NotificationLog, log_id).status == NotifStatus.FAILED


async def test_crm_dns_outage_is_retried_not_marked_blocked(db_session, monkeypatch):
    import socket

    log_id, _ = _seed(db_session, url="https://flaky-dns.example.com/hook")
    import app.workers.sales_notifications as notif_mod

    monkeypatch.setattr(notif_mod, "SessionLocal", lambda: _NoCloseSession(db_session))

    def boom(*a, **k):
        raise socket.gaierror("temporary failure")

    monkeypatch.setattr("app.utils.network_security.socket.getaddrinfo", boom)
    with patch("httpx.AsyncClient.send") as send:
        with pytest.raises(Exception):
            await sync_lead_to_crm({}, str(log_id))  # raises so ARQ retries
    send.assert_not_called()
    log = db_session.get(NotificationLog, log_id)
    assert log.status == NotifStatus.FAILED
    assert log.error != "blocked webhook host"
