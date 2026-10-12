"""Tests for VisitorService.bootstrap_visitor privacy gating."""

import uuid

import pytest

from app.db.models.tenant import Tenant
from app.schemas.visitor import VisitorBootstrapRequest
from app.services.visitor_service import VisitorService


def _make_tenant(db, *, is_publicly_listed: bool) -> Tenant:
    slug = f"t-{uuid.uuid4().hex[:8]}"
    t = Tenant(
        name=f"T_{uuid.uuid4().hex[:8]}",
        display_name="T",
        api_key_hash=f"hash_{uuid.uuid4().hex}",
        public_slug=slug,
        is_publicly_listed=is_publicly_listed,
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def test_latest_conversation_summary_with_multiple_sessions(db):
    """Regression: a visitor with >1 conversation session must return the latest,
    not raise MultipleResultsFound (which 500'd /public/visitor/bootstrap)."""
    from datetime import UTC, datetime, timedelta

    from app.db.models.conversation import ConversationSession, SessionStatus

    tenant = _make_tenant(db, is_publicly_listed=True)
    visitor_id = uuid.uuid4()
    now = datetime.now(UTC)
    for i in range(2):
        db.add(
            ConversationSession(
                tenant_id=tenant.id,
                user_identifier=f"anon-{i}",
                visitor_id=visitor_id,
                status=SessionStatus.RESOLVED,
                last_message_at=now - timedelta(hours=i),
            )
        )
    db.commit()

    svc = VisitorService(db)
    # Must not raise; returns a summary for the most-recent session.
    summary = svc._get_latest_conversation_summary(visitor_id, tenant.id)
    assert summary is not None


class TestBootstrapPrivacyGate:
    def test_bootstrap_by_slug_blocked_for_private(self, db):
        """Slug-based bootstrap is the /b/{slug} + /support/{slug} surface;
        private tenants must reject it so the sign-in gate cannot be
        bypassed by calling bootstrap directly."""
        tenant = _make_tenant(db, is_publicly_listed=False)

        svc = VisitorService(db)
        with pytest.raises(PermissionError):
            svc.bootstrap_visitor(VisitorBootstrapRequest(workspace_slug=tenant.public_slug))

    def test_bootstrap_by_id_allowed_for_private(self, db):
        """Tenant-id bootstrap is the embed-widget surface; private tenants
        keep this so they can still run Luran's widget on their own website."""
        tenant = _make_tenant(db, is_publicly_listed=False)

        svc = VisitorService(db)
        response = svc.bootstrap_visitor(VisitorBootstrapRequest(workspace_id=tenant.id))
        assert response.visitor_token
        assert response.session_token

    def test_bootstrap_by_slug_allowed_for_public(self, db):
        """Public tenants accept anonymous chat from every surface."""
        tenant = _make_tenant(db, is_publicly_listed=True)

        svc = VisitorService(db)
        response = svc.bootstrap_visitor(VisitorBootstrapRequest(workspace_slug=tenant.public_slug))
        assert response.visitor_token


class TestBootstrapFirstResponder:
    def _set(self, db, tenant, value):
        from app.db.models.tenant import TenantAIConfig

        db.add(
            TenantAIConfig(
                id=uuid.uuid4(),
                tenant_id=tenant.id,
                brand_voice={},
                escalation_keywords=[],
                max_response_tokens=250,
                grounding_requirement=True,
                first_responder=value,
            )
        )
        db.commit()
        db.refresh(tenant)

    def test_bootstrap_exposes_human_first_responder(self, db):
        tenant = _make_tenant(db, is_publicly_listed=True)
        self._set(db, tenant, "human")
        resp = VisitorService(db).bootstrap_visitor(
            VisitorBootstrapRequest(workspace_slug=tenant.public_slug)
        )
        assert resp.tenant.first_responder == "human"

    def test_bootstrap_defaults_first_responder_to_ai(self, db):
        tenant = _make_tenant(db, is_publicly_listed=True)
        resp = VisitorService(db).bootstrap_visitor(
            VisitorBootstrapRequest(workspace_slug=tenant.public_slug)
        )
        assert resp.tenant.first_responder == "ai"

    def test_public_profile_exposes_first_responder(self, db):
        tenant = _make_tenant(db, is_publicly_listed=True)
        self._set(db, tenant, "ai_with_handoff")
        profile = VisitorService(db).get_public_business_profile(tenant.public_slug)
        assert profile.first_responder == "ai_with_handoff"
