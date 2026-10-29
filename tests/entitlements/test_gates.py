"""Every plan gate, tested at its enforcement point (Postgres session with the seeded plans)."""

import csv
import io
from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from uuid import uuid4

import pytest

from app.db.models.automation import TenantAction
from app.db.models.lead import Lead
from app.db.models.notification_log import NotificationLog, NotifChannel, NotifEvent, NotifStatus
from app.db.models.quote import Quote, QuoteStatus
from app.db.models.sales_agent_config import TenantSalesAgentConfig
from app.db.models.user import UserRole
from app.schemas.automation import TenantActionCreate
from app.services.automation_service import AutomationService
from app.services.notification_service import NotificationService
from app.services.screen_recording.gating import screen_recording_allowed
from app.workers.sales_notifications import send_slack_notification, sync_lead_to_crm
from tests.entitlements.helpers import caps_of, make_tenant, make_user

GATE_BODY = {
    "code": "feature_not_in_plan",
    "feature": "sales_close",
    "min_tier": "professional",
}
SLACK = "https://hooks.slack.com/services/T/B/x"


def _sales_cfg(db, tenant, **fields):
    cfg = TenantSalesAgentConfig.default_for(tenant.id)
    for key, value in fields.items():
        setattr(cfg, key, value)
    db.add(cfg)
    db.commit()
    return cfg


# ---------------------------------------------------------------- sales config save


def test_lite_cannot_turn_on_meeting_booking(pg_client, db_session):
    tenant = make_tenant(db_session, "lite")
    _, headers = make_user(db_session, tenant)
    r = pg_client.patch(
        "/api/v1/sales/config", headers=headers, json={"meeting_booking_enabled": True}
    )
    assert r.status_code == 403
    body = r.json()
    assert {k: body[k] for k in GATE_BODY} == GATE_BODY
    assert isinstance(body["message"], str) and body["message"]
    assert "detail" not in body  # flat contract body, not the {detail: ...} wrapper


def test_lite_cannot_turn_on_quotes(pg_client, db_session):
    tenant = make_tenant(db_session, "lite")
    _, headers = make_user(db_session, tenant)
    r = pg_client.patch("/api/v1/sales/config", headers=headers, json={"quotation_enabled": True})
    assert r.status_code == 403 and r.json()["feature"] == "sales_close"
    assert (
        db_session.query(TenantSalesAgentConfig).filter_by(tenant_id=tenant.id).count() == 0
    ), "a rejected save must not leave a row behind"


@pytest.mark.parametrize(
    "payload",
    [
        {"slack_webhook_url": SLACK},
        {"crm_webhook_url": "https://hooks.example.com/crm"},
        {"notify_channels": {"email": True, "slack": True}},
        {"notify_channels": {"email": True, "crm": True}},
    ],
)
def test_lite_cannot_enable_slack_or_crm(pg_client, db_session, payload):
    tenant = make_tenant(db_session, "lite")
    _, headers = make_user(db_session, tenant)
    r = pg_client.patch("/api/v1/sales/config", headers=headers, json=payload)
    assert r.status_code == 403
    assert r.json()["feature"] == "crm_notifications" and r.json()["min_tier"] == "professional"


def test_lite_can_save_everything_that_is_not_gated(pg_client, db_session):
    tenant = make_tenant(db_session, "lite")
    _, headers = make_user(db_session, tenant)
    r = pg_client.patch(
        "/api/v1/sales/config",
        headers=headers,
        json={
            "agent_enabled": True,
            "lead_capture_enabled": True,
            "meeting_booking_enabled": False,
            "quotation_enabled": False,
            "notify_channels": {"email": True, "slack": False, "in_app": True, "crm": False},
            "notification_email": "sales@example.com",
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["agent_enabled"] is True


def test_a_kept_option_does_not_block_other_saves_after_a_downgrade(pg_client, db_session):
    tenant = make_tenant(db_session, "lite")
    _sales_cfg(db_session, tenant, meeting_booking_enabled=True, slack_webhook_url=SLACK)
    _, headers = make_user(db_session, tenant)
    r = pg_client.patch(
        "/api/v1/sales/config",
        headers=headers,
        json={"meeting_booking_enabled": True, "currency": "USD"},
    )
    assert r.status_code == 200, r.text


def test_professional_can_enable_all_gated_options(pg_client, db_session):
    tenant = make_tenant(db_session, "professional")
    _, headers = make_user(db_session, tenant)
    r = pg_client.patch(
        "/api/v1/sales/config",
        headers=headers,
        json={
            "meeting_booking_enabled": True,
            "quotation_enabled": True,
            "slack_webhook_url": SLACK,
            "notify_channels": {"email": True, "slack": True, "crm": True},
        },
    )
    assert r.status_code == 200, r.text


def test_override_flag_unlocks_a_gated_save_on_lite(pg_client, db_session):
    tenant = make_tenant(db_session, "lite")
    caps = caps_of(db_session, tenant)
    caps.can_use_sales_close = True
    caps.override_flags = {"can_use_sales_close": True}
    db_session.commit()
    _, headers = make_user(db_session, tenant)
    r = pg_client.patch("/api/v1/sales/config", headers=headers, json={"quotation_enabled": True})
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------- public meeting endpoints


def _meeting_seed(db, tenant):
    cfg = _sales_cfg(
        db,
        tenant,
        meeting_booking_enabled=True,
        availability={
            "weekdays": [1, 2, 3, 4, 5, 6, 7],
            "start": "00:00",
            "end": "23:30",
            "slot_minutes": 30,
            "buffer_minutes": 0,
            "tenant_timezone": "UTC",
        },
    )
    conv = uuid4()
    db.add(Lead(tenant_id=tenant.id, conversation_id=conv, email="a@b.com"))
    db.commit()
    return cfg, conv


def test_lite_meeting_picker_is_empty_and_booking_is_refused_like_a_disabled_option(
    pg_client, db_session
):
    tenant = make_tenant(db_session, "lite")
    _, conv = _meeting_seed(db_session, tenant)
    r = pg_client.get(f"/api/v1/chat/meetings/availability?conversation_id={conv}&days=2")
    assert r.status_code == 200 and r.json()["data"]["slots"] == []
    when = (datetime.now(UTC) + timedelta(days=1)).replace(minute=0, second=0, microsecond=0)
    r = pg_client.post(
        "/api/v1/chat/meetings",
        json={
            "conversation_id": str(conv),
            "scheduled_at": when.isoformat(),
            "visitor_timezone": "UTC",
        },
    )
    # The visitor sees exactly what a tenant with booking switched off produces: no plan details.
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "BOOKING_DISABLED"
    assert "plan" not in r.text.lower() and "feature_not_in_plan" not in r.text


def test_professional_meeting_picker_still_works(pg_client, db_session):
    tenant = make_tenant(db_session, "professional")
    _, conv = _meeting_seed(db_session, tenant)
    r = pg_client.get(f"/api/v1/chat/meetings/availability?conversation_id={conv}&days=2")
    assert r.status_code == 200 and len(r.json()["data"]["slots"]) > 0


# ---------------------------------------------------------------- chat sales branch


def _chat_with_quote(pg_client, db_session, tier):
    from tests.api.v1.test_chat_lead_capture import (
        _automation_patch,
        _retrieval_patch,
        _sales_llm_patch,
    )

    tenant = make_tenant(db_session, tier)
    _sales_cfg(
        db_session,
        tenant,
        agent_enabled=True,
        lead_capture_enabled=True,
        meeting_booking_enabled=True,
        quotation_enabled=True,
        pricing_tiers=[
            {
                "name": "Growth",
                "min_users": 11,
                "max_users": 50,
                "base_price": 45000,
                "per_seat_overage_price": 3500,
            }
        ],
        annual_discount_pct=15,
    )
    signals = {
        "asked_features": False,
        "requested_demo": True,
        "mentioned_competitor": False,
        "ready_to_buy": True,
        "offer_meeting": True,
        "offer_quote": True,
    }
    captured = {
        "name": "Alice",
        "email": "alice@example.com",
        "team_size": "58",
        "plan_interest": "Growth",
    }
    with (
        _sales_llm_patch("PRICING_INQUIRY", captured, signals),
        _retrieval_patch(),
        _automation_patch(),
    ):
        r = pg_client.post(
            "/api/v1/chat/message",
            json={
                "message": "What will Growth cost for 58 users?",
                "tenant_id": str(tenant.id),
                "user_identifier": f"gate-{uuid4().hex[:6]}",
            },
        )
    assert r.status_code == 200, r.text
    return tenant, r.json()["message"]["meta"] or {}


def test_chat_on_professional_offers_meeting_and_quote(pg_client, db_session):
    tenant, meta = _chat_with_quote(pg_client, db_session, "professional")
    assert meta.get("offer_meeting") is True and "quote" in meta
    assert db_session.query(Quote).filter_by(tenant_id=tenant.id).count() == 1


def test_chat_on_lite_skips_meeting_and_quote_but_still_captures_the_lead(pg_client, db_session):
    tenant, meta = _chat_with_quote(pg_client, db_session, "lite")
    assert "offer_meeting" not in meta and "quote" not in meta
    assert "error" not in meta  # the visitor never sees a failure
    assert db_session.query(Quote).filter_by(tenant_id=tenant.id).count() == 0
    assert db_session.query(Lead).filter_by(tenant_id=tenant.id).count() == 1


def test_sales_prompt_is_told_booking_is_off_on_lite(pg_client, db_session):
    seen = {}

    async def _spy(self, prompt, config, **kw):
        seen["booking"] = config.meeting_booking_enabled
        seen["tiers"] = config.pricing_tiers
        return "ok", {"intent": "OTHER", "captured": {}, "signals": {}}, 10, False

    from tests.api.v1.test_chat_lead_capture import _automation_patch, _retrieval_patch

    tenant = make_tenant(db_session, "lite")
    _sales_cfg(
        db_session,
        tenant,
        agent_enabled=True,
        meeting_booking_enabled=True,
        pricing_tiers=[{"name": "x"}],
    )
    with (
        patch("app.services.rag_service.RAGService.generate_sales_response", new=_spy),
        _retrieval_patch(),
        _automation_patch(),
    ):
        r = pg_client.post(
            "/api/v1/chat/message",
            json={
                "message": "What will Growth cost for 58 users?",
                "tenant_id": str(tenant.id),
                "user_identifier": f"p-{uuid4().hex[:6]}",
            },
        )
    assert r.status_code == 200, r.text
    assert seen["booking"] is False  # prompt: no meeting offers
    assert seen["tiers"] == [{"name": "x"}]  # everything else is the stored config
    cfg = db_session.query(TenantSalesAgentConfig).filter_by(tenant_id=tenant.id).one()
    assert cfg.meeting_booking_enabled is True  # the stored setting is never rewritten


# ---------------------------------------------------------------- Slack / CRM


def _lead_and_quote(db, tenant):
    lead = Lead(tenant_id=tenant.id, conversation_id=uuid4(), email="v@co.com", company="Acme")
    db.add(lead)
    db.flush()
    quote = Quote(
        tenant_id=tenant.id,
        lead_id=lead.id,
        currency="NGN",
        line_items=[{"label": "Growth", "detail": "", "amount": 45000}],
        total=45000,
        status=QuoteStatus.DRAFT,
    )
    db.add(quote)
    db.commit()
    return lead, quote


def _channels(db, tenant, **kw):
    return {c.channel for c in db.query(NotificationLog).filter_by(tenant_id=tenant.id).all()}


@pytest.mark.parametrize("tier,expected", [("lite", False), ("professional", True)])
def test_dispatch_sends_slack_and_crm_only_when_the_plan_has_them(db_session, tier, expected):
    tenant = make_tenant(db_session, tier)
    _sales_cfg(
        db_session,
        tenant,
        notify_channels={"email": False, "in_app": True, "slack": True, "crm": True},
        slack_webhook_url=SLACK,
        crm_webhook_url="https://hooks.example.com/crm",
    )
    lead, quote = _lead_and_quote(db_session, tenant)
    service = NotificationService(db_session)
    q_jobs = service.dispatch(
        tenant.id, NotifEvent.QUOTE_GENERATED, entity_type="quote", entity_id=quote.id
    )
    s_jobs = service.dispatch(
        tenant.id, NotifEvent.SALES_READY_LEAD, entity_type="lead", entity_id=lead.id
    )
    names = {name for _, name in q_jobs + s_jobs}
    assert ("send_slack_notification" in names) is expected
    assert ("sync_lead_to_crm" in names) is expected
    assert (NotifChannel.SLACK in _channels(db_session, tenant)) is expected
    assert (NotifChannel.CRM in _channels(db_session, tenant)) is expected
    # In-app notifications are never gated.
    assert NotifChannel.IN_APP in _channels(db_session, tenant)


def test_email_notifications_are_not_gated(db_session):
    tenant = make_tenant(db_session, "lite")
    _sales_cfg(
        db_session, tenant, notify_channels={"email": True}, notification_email="rep@example.com"
    )
    _, quote = _lead_and_quote(db_session, tenant)
    jobs = NotificationService(db_session).dispatch(
        tenant.id, NotifEvent.QUOTE_GENERATED, entity_type="quote", entity_id=quote.id
    )
    assert [name for _, name in jobs] == ["send_rep_alert"]


class _NoClose:
    def __init__(self, session):
        self._s = session

    def close(self):
        pass

    def __getattr__(self, name):
        return getattr(self._s, name)


def _log(db, tenant, channel, event, entity_id):
    log = NotificationLog(
        tenant_id=tenant.id,
        event=event,
        channel=channel,
        entity_type="quote" if channel == NotifChannel.SLACK else "lead",
        entity_id=entity_id,
        recipient="x",
        status=NotifStatus.PENDING,
        title="t",
        body="b",
    )
    db.add(log)
    db.commit()
    return log.id


async def test_queued_slack_and_crm_jobs_do_not_send_after_a_downgrade(db_session, monkeypatch):
    import app.workers.sales_notifications as worker

    monkeypatch.setattr(worker, "SessionLocal", lambda: _NoClose(db_session))
    tenant = make_tenant(db_session, "lite")  # downgraded after the jobs were queued
    _sales_cfg(
        db_session, tenant, slack_webhook_url=SLACK, crm_webhook_url="https://hooks.example.com/crm"
    )
    lead, quote = _lead_and_quote(db_session, tenant)
    slack_id = _log(db_session, tenant, NotifChannel.SLACK, NotifEvent.QUOTE_GENERATED, quote.id)
    crm_id = _log(db_session, tenant, NotifChannel.CRM, NotifEvent.SALES_READY_LEAD, lead.id)
    with patch("httpx.AsyncClient.send") as send:
        await send_slack_notification({}, str(slack_id))
        await sync_lead_to_crm({}, str(crm_id))
    send.assert_not_called()
    for log_id in (slack_id, crm_id):
        log = db_session.get(NotificationLog, log_id)
        assert log.status == NotifStatus.FAILED and "plan" in log.error


# ---------------------------------------------------------------- screen recording


@pytest.mark.parametrize(
    "tier,expected",
    [
        ("trial", True),
        ("lite", False),
        ("professional", True),
        ("growth", True),
        ("enterprise", True),
    ],
)
def test_screen_recording_follows_the_plan(db_session, tier, expected):
    tenant = make_tenant(db_session, tier)
    assert tenant.widget_allow_screen_recording is True  # the tenant's own switch is on by default
    assert screen_recording_allowed(db_session, tenant) is expected


def test_screen_recording_needs_both_plan_and_switch(db_session):
    tenant = make_tenant(db_session, "professional")
    tenant.widget_allow_screen_recording = False
    db_session.commit()
    assert screen_recording_allowed(db_session, tenant) is False


def test_public_widget_config_hides_recording_on_lite(pg_client, db_session):
    from tests.api.v1.test_widget_public_config_first_responder import _get_config

    lite, pro = make_tenant(db_session, "lite"), make_tenant(db_session, "professional")
    assert _get_config(pg_client, lite.id).json()["allow_screen_recording"] is False
    assert _get_config(pg_client, pro.id).json()["allow_screen_recording"] is True


def test_admin_widget_config_reports_the_plan_decision(pg_client, db_session):
    lite = make_tenant(db_session, "lite")
    _, headers = make_user(db_session, lite)
    r = pg_client.get("/api/v1/widget/config", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["can_use_screen_recording"] is False


# ---------------------------------------------------------------- ratings


def _rating(db, tenant, agent=None, score=5, comment="great"):
    from app.db.models.conversation import ConversationSession, SessionStatus
    from app.db.models.review import SessionRating

    session = ConversationSession(
        tenant_id=tenant.id, user_identifier=f"v-{uuid4().hex[:8]}", status=SessionStatus.ACTIVE
    )
    db.add(session)
    db.commit()
    db.add(
        SessionRating(
            session_id=session.id,
            tenant_id=tenant.id,
            user_identifier=session.user_identifier,
            score=score,
            comment=comment,
            rated_agent_id=agent.id if agent else None,
        )
    )
    db.commit()


def test_lite_admin_cannot_open_the_ratings_breakdown_or_export(pg_client, db_session):
    tenant = make_tenant(db_session, "lite")
    _, headers = make_user(db_session, tenant)
    for path in ("/api/v1/reviews/session", "/api/v1/reviews/session/export"):
        r = pg_client.get(path, headers=headers)
        assert r.status_code == 403, path
        assert r.json()["code"] == "feature_not_in_plan" and r.json()["feature"] == "ratings_export"
        assert r.json()["min_tier"] == "professional"


def test_lite_agent_still_sees_their_own_ratings_and_the_agent_app_view(pg_client, db_session):
    tenant = make_tenant(db_session, "lite")
    agent, headers = make_user(db_session, tenant, UserRole.SUPPORT_AGENT)
    _rating(db_session, tenant, agent=agent, score=4)
    r = pg_client.get("/api/v1/reviews/session", headers=headers)
    assert r.status_code == 200 and r.json()["total"] == 1
    assert pg_client.get("/api/v1/agent/me/ratings", headers=headers).status_code == 200


def test_lite_admin_can_view_their_own_ratings(pg_client, db_session):
    tenant = make_tenant(db_session, "lite")
    admin, headers = make_user(db_session, tenant)
    r = pg_client.get(f"/api/v1/reviews/session?rated_agent_id={admin.id}", headers=headers)
    assert r.status_code == 200


def test_rating_collection_is_never_gated(pg_client, db_session):
    """The public submit endpoint does not look at the plan (needs only a session)."""
    from app.db.models.conversation import ConversationSession, SessionStatus

    tenant = make_tenant(db_session, "lite")
    session = ConversationSession(
        tenant_id=tenant.id, user_identifier=f"v-{uuid4().hex[:8]}", status=SessionStatus.ACTIVE
    )
    db_session.add(session)
    db_session.commit()
    r = pg_client.post(
        "/api/v1/reviews/session",
        json={
            "session_id": str(session.id),
            "user_identifier": session.user_identifier,
            "score": 5,
        },
    )
    assert r.status_code == 201, r.text


def test_professional_admin_lists_and_exports_ratings(pg_client, db_session):
    tenant = make_tenant(db_session, "professional")
    _, headers = make_user(db_session, tenant)
    _rating(db_session, tenant, score=5, comment='=HYPERLINK("http://evil")')
    _rating(db_session, tenant, score=2, comment="slow")
    assert pg_client.get("/api/v1/reviews/session", headers=headers).json()["total"] == 2
    r = pg_client.get("/api/v1/reviews/session/export", headers=headers)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    rows = list(csv.reader(io.StringIO(r.text)))
    assert rows[0][:3] == ["created_at", "score", "comment"]
    assert len(rows) == 3
    comments = {row[2] for row in rows[1:]}
    assert '\'=HYPERLINK("http://evil")' in comments  # formula neutralised
    assert "slow" in comments


def test_export_only_contains_the_callers_tenant(pg_client, db_session):
    mine, other = make_tenant(db_session, "professional"), make_tenant(db_session, "professional")
    _, headers = make_user(db_session, mine)
    _rating(db_session, mine, comment="mine")
    _rating(db_session, other, comment="theirs")
    text = pg_client.get("/api/v1/reviews/session/export", headers=headers).text
    assert "mine" in text and "theirs" not in text


def test_export_is_tenant_admin_only(pg_client, db_session):
    tenant = make_tenant(db_session, "professional")
    _, headers = make_user(db_session, tenant, UserRole.SUPPORT_AGENT)
    assert pg_client.get("/api/v1/reviews/session/export", headers=headers).status_code == 403


def _agent_rows(pg_client, headers):
    r = pg_client.get("/api/v1/analytics/agents", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def test_analytics_agent_table_hides_ratings_on_lite_and_shows_them_on_professional(
    pg_client, db_session
):
    for tier, expect_count in (("lite", 0), ("professional", 1)):
        tenant = make_tenant(db_session, tier)
        _, headers = make_user(db_session, tenant)
        agent, _ = make_user(db_session, tenant, UserRole.SUPPORT_AGENT)
        _rating(db_session, tenant, agent=agent, score=5)
        rows = [r for r in _agent_rows(pg_client, headers) if r["agent_id"] == str(agent.id)]
        assert rows[0]["ratings_count"] == expect_count, tier
        if tier == "lite":
            assert rows[0]["average_rating"] is None and rows[0]["csat_percent"] is None


# ---------------------------------------------------------------- AI actions, lookup chains


def _action(i):
    return TenantActionCreate(
        name=f"Action {i}",
        slug=f"action_{i}_{uuid4().hex[:6]}",
        description="d",
        method="GET",
        url_template="https://api.example.com/x",
        input_schema={},
    )


@pytest.mark.parametrize("tier,limit", [("trial", 2), ("lite", 2), ("professional", 15)])
def test_ai_action_limit_is_plan_driven(db_session, tier, limit):
    tenant = make_tenant(db_session, tier)
    service = AutomationService(db_session)
    with patch.object(service, "count_actions", return_value=limit - 1):
        service._check_action_limit(tenant.id)  # one slot left: fine
    with patch.object(service, "count_actions", return_value=limit):
        with pytest.raises(ValueError, match=f"allows {limit} automation actions"):
            service._check_action_limit(tenant.id)


def test_enterprise_has_no_action_limit(db_session):
    tenant = make_tenant(db_session, "enterprise")
    service = AutomationService(db_session)
    with patch.object(service, "count_actions", return_value=10_000):
        service._check_action_limit(tenant.id)


def test_a_platform_override_raises_the_action_limit(db_session):
    tenant = make_tenant(db_session, "lite")
    caps = caps_of(db_session, tenant)
    caps.max_automation_actions = 9
    caps.override_flags = {"max_automation_actions": True}
    db_session.commit()
    service = AutomationService(db_session)
    with patch.object(service, "count_actions", return_value=8):
        service._check_action_limit(tenant.id)
    assert db_session.query(TenantAction).filter_by(tenant_id=tenant.id).count() == 0


def test_chat_automation_orchestrator_reads_the_resolver(db_session):
    from app.services.chat_automation_orchestrator import ChatAutomationOrchestrator

    lite, pro = make_tenant(db_session, "lite"), make_tenant(db_session, "professional")
    orchestrator = ChatAutomationOrchestrator(db_session)
    assert orchestrator._can_use_lookup_chains(lite.id) is False
    assert orchestrator._can_use_lookup_chains(pro.id) is True
    assert orchestrator._can_use_ai_chat_automation(lite.id) is True


def test_analytics_page_is_closed_on_trial_and_open_on_lite(pg_client, db_session):
    trial, lite = make_tenant(db_session, "trial"), make_tenant(db_session, "lite")
    _, trial_headers = make_user(db_session, trial)
    _, lite_headers = make_user(db_session, lite)
    assert pg_client.get("/api/v1/analytics/overview", headers=trial_headers).status_code == 403
    assert pg_client.get("/api/v1/analytics/overview", headers=lite_headers).status_code == 200


# ---------------------------------------------------------------- downgrade conflicts


def test_downgrade_is_blocked_while_gated_options_are_on(db_session):
    from app.services.subscription_validation_service import SubscriptionValidationService

    tenant = make_tenant(db_session, "professional")
    _sales_cfg(
        db_session,
        tenant,
        meeting_booking_enabled=True,
        quotation_enabled=True,
        notify_channels={"email": True, "slack": True, "crm": True},
    )
    result = SubscriptionValidationService(db_session).validate_tier_change(tenant.id, "lite")
    fields = {v.field: v for v in result.violations}
    assert result.valid is False
    assert fields["sales_close"].current_value == 2 and fields["sales_close"].allowed_value == 0
    assert fields["crm_notifications"].current_value == 2
    assert "Turn off" in fields["sales_close"].action_required
    # Same data is fine on a plan that includes the features.
    assert SubscriptionValidationService(db_session).validate_tier_change(tenant.id, "growth").valid


def test_downgrade_is_clean_when_the_options_are_off(db_session):
    from app.services.subscription_validation_service import SubscriptionValidationService

    tenant = make_tenant(db_session, "professional")
    _sales_cfg(db_session, tenant, notify_channels={"email": True, "slack": False, "crm": False})
    assert SubscriptionValidationService(db_session).validate_tier_change(tenant.id, "lite").valid


def test_downgrade_checks_linked_whatsapp_numbers(db_session):
    from app.db.models.whatsapp import WhatsAppConnection
    from app.services.subscription_validation_service import SubscriptionValidationService
    from app.utils.datetime import utcnow_aware

    tenant = make_tenant(db_session, "growth")
    for i in range(2):
        phone = f"23480000010{i}0"
        db_session.add(
            WhatsAppConnection(
                tenant_id=tenant.id,
                status="connected",
                phone_e164=phone,
                phone_hmac=uuid4().hex,
                session_key=f"sk_{uuid4().hex[:16]}",
                consent_version="v",
                consent_accepted_at=utcnow_aware(),
            )
        )
    db_session.commit()
    result = SubscriptionValidationService(db_session).validate_tier_change(tenant.id, "lite")
    violation = {v.field: v for v in result.violations}["whatsapp_numbers"]
    assert (violation.current_value, violation.allowed_value, violation.difference) == (2, 1, 1)
    assert "Disconnect 1 WhatsApp number" in violation.action_required
    assert "whatsapp_numbers" not in {
        v.field
        for v in SubscriptionValidationService(db_session)
        .validate_tier_change(tenant.id, "professional")
        .violations
    }


def _link_numbers(db_session, tenant, count: int) -> None:
    from app.db.models.whatsapp import WhatsAppConnection
    from app.utils.datetime import utcnow_aware

    for i in range(count):
        db_session.add(
            WhatsAppConnection(
                tenant_id=tenant.id,
                status="connected",
                phone_e164=f"23480000020{i}0",
                phone_hmac=uuid4().hex,
                session_key=f"sk_{uuid4().hex[:16]}",
                consent_version="v",
                consent_accepted_at=utcnow_aware(),
            )
        )
    db_session.commit()


def test_downgrade_to_a_plan_without_whatsapp_is_blocked_while_numbers_are_linked(db_session):
    from app.db.models.subscription_plan import SubscriptionPlan
    from app.services.subscription_validation_service import SubscriptionValidationService

    tenant = make_tenant(db_session, "growth")
    _link_numbers(db_session, tenant, 1)
    db_session.query(SubscriptionPlan).filter_by(tier="lite").update({"can_use_whatsapp": False})
    db_session.commit()
    result = SubscriptionValidationService(db_session).validate_tier_change(tenant.id, "lite")
    violation = {v.field: v for v in result.violations}["whatsapp_numbers"]
    # Lite still lists one number, but without the flag nothing is allowed.
    assert (violation.current_value, violation.allowed_value, violation.difference) == (1, 0, 1)
    assert "Disconnect 1 WhatsApp number" in violation.action_required


def test_downgrade_to_a_plan_without_whatsapp_is_clean_when_nothing_is_linked(db_session):
    from app.db.models.subscription_plan import SubscriptionPlan
    from app.services.subscription_validation_service import SubscriptionValidationService

    tenant = make_tenant(db_session, "growth")
    db_session.query(SubscriptionPlan).filter_by(tier="lite").update({"can_use_whatsapp": False})
    db_session.commit()
    result = SubscriptionValidationService(db_session).validate_tier_change(tenant.id, "lite")
    assert "whatsapp_numbers" not in {v.field for v in result.violations}


def test_downgrade_between_plans_that_include_whatsapp_keeps_the_number_check_only(db_session):
    from app.services.subscription_validation_service import SubscriptionValidationService

    tenant = make_tenant(db_session, "growth")
    _link_numbers(db_session, tenant, 1)
    result = SubscriptionValidationService(db_session).validate_tier_change(tenant.id, "lite")
    assert "whatsapp_numbers" not in {v.field for v in result.violations}  # 1 number fits Lite
