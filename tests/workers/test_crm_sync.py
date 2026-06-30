"""Tests for the CRM webhook ARQ job (Stage 3 Slice 5e Task 3)."""
import pytest
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
    t = Tenant(name=f"t_{uuid4().hex[:8]}", display_name="Acme", api_key_hash=uuid4().hex,
               subscription_tier="trial", subscription_status="trialing")
    db.add(t); db.flush()
    cfg = TenantSalesAgentConfig.default_for(t.id)
    cfg.crm_webhook_url = url
    db.add(cfg)
    lead = Lead(tenant_id=t.id, conversation_id=uuid4(), name="Dana", email="d@co.com",
                company="Acme", team_size="11-50", industry="SaaS", intent="DEMO_REQUEST",
                lead_score=90, source="chat")
    db.add(lead); db.flush()
    log = NotificationLog(tenant_id=t.id, event=NotifEvent.SALES_READY_LEAD,
                          channel=NotifChannel.CRM, entity_type="lead", entity_id=lead.id,
                          recipient="crm", status=NotifStatus.PENDING,
                          title="New sales-ready lead", body="Dana · Acme · score 90")
    db.add(log); db.commit()
    return log.id, lead.id


def _resp(code, json_body=None):
    r = MagicMock()
    r.status_code = code
    if json_body is None:
        r.json.side_effect = ValueError("no json")
    else:
        r.json.return_value = json_body
    return r


async def test_crm_2xx_marks_sent_and_posts_lead_payload(db_session, monkeypatch):
    log_id, lead_id = _seed(db_session)
    import app.workers.sales_notifications as notif_mod
    monkeypatch.setattr(notif_mod, "SessionLocal", lambda: _NoCloseSession(db_session))

    with patch("httpx.Client.post", return_value=_resp(200)) as post:
        await sync_lead_to_crm({}, str(log_id))

    post.assert_called_once()
    call = post.call_args
    url = call[0][0] if call[0] else call[1]["url"]
    payload = call[1]["json"]
    assert url == "https://hooks.zapier.com/hooks/catch/1/x"
    assert payload["email"] == "d@co.com"
    assert payload["company"] == "Acme"
    assert payload["lead_score"] == 90
    assert payload["lead_id"] == str(lead_id)
    assert db_session.get(NotificationLog, log_id).status == NotifStatus.SENT


async def test_crm_2xx_with_id_writes_back_crm_id(db_session, monkeypatch):
    log_id, lead_id = _seed(db_session)
    import app.workers.sales_notifications as notif_mod
    monkeypatch.setattr(notif_mod, "SessionLocal", lambda: _NoCloseSession(db_session))

    with patch("httpx.Client.post", return_value=_resp(200, {"id": "crm-789"})):
        await sync_lead_to_crm({}, str(log_id))

    assert db_session.get(NotificationLog, log_id).status == NotifStatus.SENT
    assert db_session.get(Lead, lead_id).crm_id == "crm-789"


async def test_crm_2xx_without_id_leaves_crm_id_none(db_session, monkeypatch):
    log_id, lead_id = _seed(db_session)
    import app.workers.sales_notifications as notif_mod
    monkeypatch.setattr(notif_mod, "SessionLocal", lambda: _NoCloseSession(db_session))

    with patch("httpx.Client.post", return_value=_resp(200, {"ok": True})):
        await sync_lead_to_crm({}, str(log_id))

    assert db_session.get(NotificationLog, log_id).status == NotifStatus.SENT
    assert db_session.get(Lead, lead_id).crm_id is None


async def test_crm_5xx_raises_for_retry(db_session, monkeypatch):
    log_id, _ = _seed(db_session)
    import app.workers.sales_notifications as notif_mod
    monkeypatch.setattr(notif_mod, "SessionLocal", lambda: _NoCloseSession(db_session))

    with patch("httpx.Client.post", return_value=_resp(503)):
        with pytest.raises(Exception):
            await sync_lead_to_crm({}, str(log_id))

    assert db_session.get(NotificationLog, log_id).status == NotifStatus.FAILED


async def test_crm_4xx_no_retry(db_session, monkeypatch):
    log_id, _ = _seed(db_session)
    import app.workers.sales_notifications as notif_mod
    monkeypatch.setattr(notif_mod, "SessionLocal", lambda: _NoCloseSession(db_session))

    with patch("httpx.Client.post", return_value=_resp(404)):
        await sync_lead_to_crm({}, str(log_id))  # must NOT raise

    assert db_session.get(NotificationLog, log_id).status == NotifStatus.FAILED


async def test_crm_missing_url_marks_failed(db_session, monkeypatch):
    log_id, _ = _seed(db_session, url=None)
    import app.workers.sales_notifications as notif_mod
    monkeypatch.setattr(notif_mod, "SessionLocal", lambda: _NoCloseSession(db_session))

    with patch("httpx.Client.post") as post:
        await sync_lead_to_crm({}, str(log_id))

    post.assert_not_called()
    assert db_session.get(NotificationLog, log_id).status == NotifStatus.FAILED


async def test_crm_already_sent_is_noop(db_session, monkeypatch):
    log_id, _ = _seed(db_session)
    db_session.get(NotificationLog, log_id).status = NotifStatus.SENT
    db_session.commit()
    import app.workers.sales_notifications as notif_mod
    monkeypatch.setattr(notif_mod, "SessionLocal", lambda: _NoCloseSession(db_session))

    with patch("httpx.Client.post") as post:
        await sync_lead_to_crm({}, str(log_id))

    post.assert_not_called()
    assert db_session.get(NotificationLog, log_id).status == NotifStatus.SENT


async def test_crm_blocked_host_marks_failed_no_post(db_session, monkeypatch):
    log_id, _ = _seed(db_session, url="http://169.254.169.254/latest/meta-data/")
    import app.workers.sales_notifications as notif_mod
    monkeypatch.setattr(notif_mod, "SessionLocal", lambda: _NoCloseSession(db_session))
    with patch("httpx.Client.post") as post:
        await sync_lead_to_crm({}, str(log_id))  # must NOT raise
    post.assert_not_called()
    log = db_session.get(NotificationLog, log_id)
    assert log.status == NotifStatus.FAILED
    assert log.error == "blocked webhook host"
