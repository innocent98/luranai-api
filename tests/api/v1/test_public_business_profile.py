"""API tests for GET /api/v1/public/business/{slug} — allow_file_upload gate flag."""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db.models.tenant import Tenant


def _make_tenant(db: Session, *, allow_file_upload: bool) -> Tenant:
    tenant = Tenant(
        name=f"profile_{uuid.uuid4().hex[:8]}",
        display_name="Profile Co",
        api_key_hash=f"hash_{uuid.uuid4().hex}",
        public_slug=f"profile-{uuid.uuid4().hex[:6]}",
        is_publicly_listed=True,
        widget_allow_file_upload=allow_file_upload,
    )
    db.add(tenant)
    db.commit()
    db.refresh(tenant)
    return tenant


@pytest.mark.parametrize("allow_file_upload", [True, False])
def test_public_profile_reflects_allow_file_upload(
    client: TestClient, db: Session, allow_file_upload: bool
):
    """The gate flag on the public profile is wired to the real tenant column."""
    tenant = _make_tenant(db, allow_file_upload=allow_file_upload)

    resp = client.get(f"/api/v1/public/business/{tenant.public_slug}")

    assert resp.status_code == 200
    assert resp.json()["allow_file_upload"] is allow_file_upload


@pytest.mark.parametrize("listed", [True, False])
def test_public_profile_reports_real_first_responder_even_when_private(
    client: TestClient, db: Session, listed: bool
):
    """N3 follow-up: a private tenant must not report the schema default 'ai'."""
    from app.db.models.tenant import TenantAIConfig

    tenant = _make_tenant(db, allow_file_upload=True)
    tenant.is_publicly_listed = listed
    db.add(TenantAIConfig(tenant_id=tenant.id, first_responder="human"))
    db.commit()

    resp = client.get(f"/api/v1/public/business/{tenant.public_slug}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["is_private"] is (not listed)
    assert body["first_responder"] == "human"
    if not listed:
        # Still redacted: only the mode string is added, nothing descriptive.
        assert body["description"] is None
        assert body["widget_greeting_message"] is None
