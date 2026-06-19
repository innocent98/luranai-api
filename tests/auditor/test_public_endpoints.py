"""Tests for public auditor endpoints (no-auth, rate-limited)."""
import uuid
from unittest.mock import patch

from app.db.models.audit import AuditReport, AuditScope, AuditStatus
from app.db.models.lead import Lead


def test_post_public_audit_starts_and_returns_id(client, db):
    """POST /public/audit creates a report and returns audit_report_id."""
    with patch("app.auditor.service.enqueue_audit"):
        resp = client.post("/api/v1/public/audit", json={"url": "https://example.com"})
    assert resp.status_code == 202
    body = resp.json()
    assert body["success"] is True
    assert "audit_report_id" in body["data"]


def test_post_public_audit_rate_limit_enforced(client, db):
    """After 3 audits from same IP, 4th should return 429."""
    from fastapi import HTTPException, Request, status

    from app.api.v1.endpoints.public_auditor import public_audit_rate_limit
    from app.main import app

    call_count = {"n": 0}

    async def fake_rate_limit(request: Request) -> None:
        call_count["n"] += 1
        if call_count["n"] > 3:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "error": "Rate limit exceeded",
                    "limit": 3,
                    "window_seconds": 86400,
                    "retry_after_seconds": 86400,
                },
                headers={"Retry-After": "86400"},
            )

    app.dependency_overrides[public_audit_rate_limit] = fake_rate_limit
    try:
        with patch("app.auditor.service.enqueue_audit"):
            for i in range(3):
                resp = client.post(
                    "/api/v1/public/audit", json={"url": f"https://example{i}.com"}
                )
                assert resp.status_code == 202, (
                    f"Expected 202 on call {i + 1}, got {resp.status_code}: {resp.json()}"
                )

            # 4th call — should be rate limited
            resp4 = client.post("/api/v1/public/audit", json={"url": "https://blocked.com"})
            assert resp4.status_code == 429
    finally:
        del app.dependency_overrides[public_audit_rate_limit]


def test_get_public_report_returns_scores_and_blurred_gaps(client, db):
    """GET /public/audit/{id} returns scores + blurred gap titles, NOT raw content."""
    report = AuditReport(
        url="https://ex.com",
        domain="ex.com",
        scope=AuditScope.PUBLIC,
        status=AuditStatus.COMPLETED,
        overall_score=74,
        projected_score=91,
        knowledge_score=70,
        support_score=60,
        conversion_score=80,
        gaps=[
            {
                "title": "Refund policy",
                "why_it_matters": "trust",
                "recommended_content": "secret detail",
                "kind": "policy",
                "demand_count": 3,
            }
        ],
    )
    db.add(report)
    db.commit()

    resp = client.get(f"/api/v1/public/audit/{report.id}")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["overall_score"] == 74
    assert data["projected_score"] == 91
    assert len(data["gaps"]) == 1
    gap = data["gaps"][0]
    assert gap["title"] == "Refund policy"
    assert gap["kind"] == "policy"
    assert gap["demand_count"] == 3
    # Strict: no raw content must leak
    assert "recommended_content" not in gap
    assert "why_it_matters" not in gap


def test_get_public_report_not_found(client, db):
    """GET /public/audit/{id} returns 404 for unknown id."""
    resp = client.get(f"/api/v1/public/audit/{uuid.uuid4()}")
    assert resp.status_code == 404
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "AUDIT_NOT_FOUND"


def test_post_email_creates_lead(client, db):
    """POST /public/audit/{id}/email creates a Lead in DB and calls EmailService."""
    report = AuditReport(
        url="https://ex.com",
        domain="ex.com",
        scope=AuditScope.PUBLIC,
        status=AuditStatus.COMPLETED,
        overall_score=74,
    )
    db.add(report)
    db.commit()

    with patch(
        "app.api.v1.endpoints.public_auditor.email_service.send_templated_email",
        return_value=True,
    ) as mock_email:
        resp = client.post(
            f"/api/v1/public/audit/{report.id}/email",
            json={"email": "owner@example.com"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert "lead_id" in body["data"]

    leads = db.query(Lead).filter(Lead.audit_report_id == report.id).all()
    assert len(leads) == 1
    assert leads[0].source == "public_auditor"

    mock_email.assert_called_once()


def test_post_email_report_not_found(client, db):
    """POST /public/audit/{id}/email returns 404 for unknown report."""
    resp = client.post(
        f"/api/v1/public/audit/{uuid.uuid4()}/email",
        json={"email": "owner@example.com"},
    )
    assert resp.status_code == 404
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "AUDIT_NOT_FOUND"
