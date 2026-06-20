"""Tests for company registration endpoint — including audit claim path."""

from unittest.mock import AsyncMock, patch

from app.db.models.audit import AuditReport, AuditScope, AuditStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_VALID_PAYLOAD = {
    "company_name": "Acme Corp",
    "admin_full_name": "Alice Admin",
    "admin_email": "alice@acme.example.com",
}

_EMAIL_PATCH = "app.services.company_registration_service.email_service.send_verification_email"
_TOKEN_PATCH = "app.services.company_registration_service.token_service.create_email_verification_token"


def _base_patches():
    """Return a list of context-manager patches needed for every registration call."""
    return [
        patch(_EMAIL_PATCH, return_value=True),
        patch(_TOKEN_PATCH, return_value="tok-abc"),
    ]


# ---------------------------------------------------------------------------
# Regression: registration WITHOUT claim_token (existing behaviour unchanged)
# ---------------------------------------------------------------------------


def test_register_company_no_claim_token(client, db):
    """POST /register-company without claim_token succeeds and returns 201."""
    with patch(_EMAIL_PATCH, return_value=True), patch(_TOKEN_PATCH, return_value="tok-1"):
        resp = client.post("/api/v1/auth/register-company", json=_VALID_PAYLOAD)

    assert resp.status_code == 201, resp.json()
    body = resp.json()
    assert "tenant_id" in body
    assert body["verification_required"] is True


# ---------------------------------------------------------------------------
# Registration WITH a valid claim_token → audit is claimed
# ---------------------------------------------------------------------------


def test_register_company_with_valid_claim_token(client, db):
    """Registration with a valid claim_token re-parents the AuditReport to the new tenant."""
    token = "valid-claim-token-abc123"

    # Create an unclaimed public audit report in the DB
    report = AuditReport(
        url="https://acme.example.com",
        domain="acme.example.com",
        scope=AuditScope.PUBLIC,
        status=AuditStatus.COMPLETED,
        overall_score=80,
        knowledge_score=75,
        support_score=70,
        conversion_score=85,
        projected_score=92,
        claim_token=token,
        tenant_id=None,  # unclaimed
    )
    db.add(report)
    db.commit()

    payload = {**_VALID_PAYLOAD, "claim_token": token}

    with (
        patch(_EMAIL_PATCH, return_value=True),
        patch(_TOKEN_PATCH, return_value="tok-2"),
        patch(
            "app.auditor.service.enqueue_audit",
            new_callable=AsyncMock,
        ),
    ):
        resp = client.post("/api/v1/auth/register-company", json=payload)

    assert resp.status_code == 201, resp.json()
    body = resp.json()
    assert "tenant_id" in body

    # The audit report must now belong to the new tenant
    db.expire_all()
    refreshed = db.query(AuditReport).filter(AuditReport.id == report.id).one()
    assert str(refreshed.tenant_id) == body["tenant_id"], (
        f"Expected tenant_id={body['tenant_id']}, got {refreshed.tenant_id}"
    )


# ---------------------------------------------------------------------------
# Registration WITH an invalid/unknown claim_token → signup still succeeds
# ---------------------------------------------------------------------------


def test_register_company_with_invalid_claim_token_still_succeeds(client, db):
    """Registration with an unknown claim_token must succeed (200/201) — not raise."""
    payload = {
        **_VALID_PAYLOAD,
        "admin_email": "bob@invalid-claim.example.com",
        "company_name": "InvalidClaimCo",
        "claim_token": "does-not-exist-token",
    }

    with (
        patch(_EMAIL_PATCH, return_value=True),
        patch(_TOKEN_PATCH, return_value="tok-3"),
    ):
        resp = client.post("/api/v1/auth/register-company", json=payload)

    # Signup MUST succeed even though the token is bogus
    assert resp.status_code == 201, resp.json()
    body = resp.json()
    assert "tenant_id" in body
    assert body["verification_required"] is True


# ---------------------------------------------------------------------------
# Registration WITH a claim_token that raises an unexpected exception → still succeeds
# ---------------------------------------------------------------------------


def test_register_company_claim_exception_swallowed(client, db):
    """Even if claim_audit raises an unexpected error, signup succeeds."""
    payload = {
        **_VALID_PAYLOAD,
        "admin_email": "carol@claim-error.example.com",
        "company_name": "ClaimErrorCo",
        "claim_token": "some-token",
    }

    with (
        patch(_EMAIL_PATCH, return_value=True),
        patch(_TOKEN_PATCH, return_value="tok-4"),
        patch(
            "app.api.v1.endpoints.company_registration.AuditService.claim_audit",
            new_callable=AsyncMock,
            side_effect=RuntimeError("Redis exploded"),
        ),
    ):
        resp = client.post("/api/v1/auth/register-company", json=payload)

    assert resp.status_code == 201, resp.json()
    body = resp.json()
    assert "tenant_id" in body


# ---------------------------------------------------------------------------
# Existing-conflict guard: duplicate company name still returns 400
# ---------------------------------------------------------------------------


def test_register_company_duplicate_name_returns_400(client, db):
    """Duplicate company name still raises 400 (regression guard)."""
    with patch(_EMAIL_PATCH, return_value=True), patch(_TOKEN_PATCH, return_value="tok-5"):
        r1 = client.post(
            "/api/v1/auth/register-company",
            json={
                "company_name": "DuplicateCo",
                "admin_full_name": "Dave",
                "admin_email": "dave@dup.example.com",
            },
        )
    assert r1.status_code == 201

    with patch(_EMAIL_PATCH, return_value=True), patch(_TOKEN_PATCH, return_value="tok-6"):
        r2 = client.post(
            "/api/v1/auth/register-company",
            json={
                "company_name": "DuplicateCo",
                "admin_full_name": "Eve",
                "admin_email": "eve@dup.example.com",
            },
        )
    assert r2.status_code == 400
