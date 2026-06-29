"""Tests for ARQ Slack notification job (Stage 3 Slice 5d)."""

import pytest
from uuid import uuid4
from unittest.mock import patch, MagicMock

from app.db.models.tenant import Tenant
from app.db.models.lead import Lead
from app.db.models.quote import Quote, QuoteStatus
from app.db.models.sales_agent_config import TenantSalesAgentConfig
from app.db.models.notification_log import NotificationLog, NotifEvent, NotifChannel, NotifStatus
from app.workers.sales_notifications import send_slack_notification


class _NoCloseSession:
    """Thin proxy around a SQLAlchemy Session that swallows close() calls.

    The worker always calls db.close() in its finally block. Without this
    proxy the test session would be closed mid-test, making the post-call
    assertion fail with a detached-instance error.
    """

    def __init__(self, session):
        self._session = session

    def close(self):
        pass  # do not close the shared test session

    def __getattr__(self, name):
        return getattr(self._session, name)


def _seed(db, *, url="https://hooks.slack.com/services/T/B/x"):
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
    cfg.slack_webhook_url = url
    db.add(cfg)

    lead = Lead(tenant_id=t.id, conversation_id=uuid4(), email="v@co.com", company="Acme")
    db.add(lead)
    db.flush()

    q = Quote(
        tenant_id=t.id,
        lead_id=lead.id,
        currency="NGN",
        line_items=[{"label": "Growth", "detail": "", "amount": 45000}],
        total=45000,
        status=QuoteStatus.DRAFT,
    )
    db.add(q)
    db.flush()

    log = NotificationLog(
        tenant_id=t.id,
        event=NotifEvent.QUOTE_GENERATED,
        channel=NotifChannel.SLACK,
        entity_type="quote",
        entity_id=q.id,
        recipient="slack",
        status=NotifStatus.PENDING,
        title="New quote",
        body="₦45,000 · Acme",
    )
    db.add(log)
    db.commit()
    return log.id


def _resp(code):
    r = MagicMock()
    r.status_code = code
    return r


async def test_slack_2xx_marks_sent(db_session, monkeypatch):
    """2xx response → status SENT; POST called with header + section blocks."""
    log_id = _seed(db_session)

    import app.workers.sales_notifications as notif_mod

    monkeypatch.setattr(notif_mod, "SessionLocal", lambda: _NoCloseSession(db_session))

    with patch("httpx.Client.post", return_value=_resp(200)) as post:
        await send_slack_notification({}, str(log_id))

    post.assert_called_once()
    call_kwargs = post.call_args
    payload = call_kwargs[1]["json"] if call_kwargs[1] else call_kwargs[0][1]
    blocks = payload["blocks"]
    assert any(b["type"] == "header" for b in blocks)
    assert any(b["type"] == "section" for b in blocks)
    assert db_session.get(NotificationLog, log_id).status == NotifStatus.SENT


async def test_slack_5xx_raises_for_retry(db_session, monkeypatch):
    """5xx response → status FAILED and raises (ARQ retry signal)."""
    log_id = _seed(db_session)

    import app.workers.sales_notifications as notif_mod

    monkeypatch.setattr(notif_mod, "SessionLocal", lambda: _NoCloseSession(db_session))

    with patch("httpx.Client.post", return_value=_resp(503)):
        with pytest.raises(Exception):
            await send_slack_notification({}, str(log_id))

    assert db_session.get(NotificationLog, log_id).status == NotifStatus.FAILED


async def test_slack_4xx_no_retry(db_session, monkeypatch):
    """4xx response → status FAILED and does NOT raise (permanent client error)."""
    log_id = _seed(db_session)

    import app.workers.sales_notifications as notif_mod

    monkeypatch.setattr(notif_mod, "SessionLocal", lambda: _NoCloseSession(db_session))

    with patch("httpx.Client.post", return_value=_resp(404)):
        await send_slack_notification({}, str(log_id))  # must NOT raise

    assert db_session.get(NotificationLog, log_id).status == NotifStatus.FAILED


async def test_slack_missing_url_marks_failed(db_session, monkeypatch):
    """Missing slack_webhook_url → status FAILED, no HTTP call made."""
    log_id = _seed(db_session, url=None)

    import app.workers.sales_notifications as notif_mod

    monkeypatch.setattr(notif_mod, "SessionLocal", lambda: _NoCloseSession(db_session))

    with patch("httpx.Client.post") as post:
        await send_slack_notification({}, str(log_id))

    post.assert_not_called()
    assert db_session.get(NotificationLog, log_id).status == NotifStatus.FAILED


async def test_slack_already_sent_is_noop(db_session, monkeypatch):
    """Already-SENT log → exits early without any HTTP call."""
    log_id = _seed(db_session)
    log = db_session.get(NotificationLog, log_id)
    log.status = NotifStatus.SENT
    db_session.commit()

    import app.workers.sales_notifications as notif_mod

    monkeypatch.setattr(notif_mod, "SessionLocal", lambda: _NoCloseSession(db_session))

    with patch("httpx.Client.post") as post:
        await send_slack_notification({}, str(log_id))

    post.assert_not_called()
    assert db_session.get(NotificationLog, log_id).status == NotifStatus.SENT
