"""GET /api/v1/leads/{id} — real CRM delivery status.

The detail payload only carried crm.connected = bool(lead.crm_id). crm_id is
written back solely when the webhook echoes an id-ish key, and Zapier/Make —
the documented way to connect a CRM — answer 2xx with no id. So a lead that
delivered successfully reported "not connected" to the dashboard.

The truth already exists in NotificationLog (channel=crm, entity_id=lead.id):
pending / sent / failed, plus the error. Expose that instead of inferring
delivery from an id the CRM may never return.

connected stays on the payload, unchanged, because /api/v1 has other clients —
this is additive.
"""

import uuid

from app.db.models.notification_log import (
    NotifChannel,
    NotifEvent,
    NotificationLog,
    NotifStatus,
)

from .test_leads_sort_helpers import auth, make_tenant_user


def _make_lead(db_session, tenant_id, *, crm_id=None):
    from app.db.models.lead import Lead, LeadStatus

    lead = Lead(
        tenant_id=tenant_id,
        name="Ada",
        email=f"ada_{uuid.uuid4().hex[:6]}@co.com",
        status=LeadStatus.NEW,
        lead_score=90,
        crm_id=crm_id,
    )
    db_session.add(lead)
    db_session.flush()
    return lead


def _log_crm(db_session, tenant_id, lead_id, status, *, error=None):
    db_session.add(
        NotificationLog(
            tenant_id=tenant_id,
            event=NotifEvent.SALES_READY_LEAD,
            channel=NotifChannel.CRM,
            entity_type="lead",
            entity_id=lead_id,
            recipient="https://hooks.zapier.com/x",
            status=status,
            error=error,
        )
    )
    db_session.flush()


def _get(pg_client, token, lead_id):
    return pg_client.get(f"/api/v1/leads/{lead_id}", headers=auth(token))


class TestCrmDeliveryStatus:
    def test_delivered_without_a_returned_id_still_reports_sent(self, pg_client, db_session):
        """The Zapier/Make case: 2xx, no id echoed back, so crm_id stays null.
        The lead DID reach the CRM and must not be reported as unsynced."""
        user, token = make_tenant_user(db_session)
        lead = _make_lead(db_session, user.tenant_id, crm_id=None)
        _log_crm(db_session, user.tenant_id, lead.id, NotifStatus.SENT)

        resp = _get(pg_client, token, lead.id)

        assert resp.status_code == 200, resp.text
        crm = resp.json()["data"]["crm"]
        assert crm["status"] == "sent"
        assert crm["synced_at"] is not None
        assert crm["error"] is None

    def test_failed_delivery_is_reported_with_its_reason(self, pg_client, db_session):
        user, token = make_tenant_user(db_session)
        lead = _make_lead(db_session, user.tenant_id)
        _log_crm(
            db_session, user.tenant_id, lead.id, NotifStatus.FAILED,
            error="no crm webhook configured",
        )

        resp = _get(pg_client, token, lead.id)

        assert resp.status_code == 200, resp.text
        crm = resp.json()["data"]["crm"]
        assert crm["status"] == "failed"
        assert crm["error"] == "no crm webhook configured"

    def test_pending_delivery_is_reported(self, pg_client, db_session):
        user, token = make_tenant_user(db_session)
        lead = _make_lead(db_session, user.tenant_id)
        _log_crm(db_session, user.tenant_id, lead.id, NotifStatus.PENDING)

        resp = _get(pg_client, token, lead.id)

        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["crm"]["status"] == "pending"

    def test_never_attempted_reports_null_not_a_failure(self, pg_client, db_session):
        """No CRM log at all means the push was never attempted — distinct from
        'it was attempted and failed'. The UI must be able to tell them apart."""
        user, token = make_tenant_user(db_session)
        lead = _make_lead(db_session, user.tenant_id)

        resp = _get(pg_client, token, lead.id)

        assert resp.status_code == 200, resp.text
        crm = resp.json()["data"]["crm"]
        assert crm["status"] is None
        assert crm["synced_at"] is None
        assert crm["error"] is None

    def test_other_channels_do_not_leak_into_crm_status(self, pg_client, db_session):
        """A Slack or email notification for the same lead is not a CRM push."""
        user, token = make_tenant_user(db_session)
        lead = _make_lead(db_session, user.tenant_id)
        db_session.add(
            NotificationLog(
                tenant_id=user.tenant_id,
                event=NotifEvent.SALES_READY_LEAD,
                channel=NotifChannel.SLACK,
                entity_type="lead",
                entity_id=lead.id,
                recipient="https://hooks.slack.com/x",
                status=NotifStatus.SENT,
            )
        )
        db_session.flush()

        resp = _get(pg_client, token, lead.id)

        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["crm"]["status"] is None

    def test_another_leads_crm_push_does_not_leak(self, pg_client, db_session):
        """entity_id must scope the lookup — one lead's push is not another's."""
        user, token = make_tenant_user(db_session)
        lead = _make_lead(db_session, user.tenant_id)
        other = _make_lead(db_session, user.tenant_id)
        _log_crm(db_session, user.tenant_id, other.id, NotifStatus.SENT)

        resp = _get(pg_client, token, lead.id)

        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["crm"]["status"] is None


class TestBackwardCompatibility:
    def test_connected_still_present_and_unchanged(self, pg_client, db_session):
        """connected keeps its bool(crm_id) meaning — /api/v1 has other clients,
        so widening this payload must stay additive."""
        user, token = make_tenant_user(db_session)
        lead = _make_lead(db_session, user.tenant_id, crm_id="hub_123")

        resp = _get(pg_client, token, lead.id)

        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["crm"]["connected"] is True
