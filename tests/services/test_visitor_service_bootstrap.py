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
