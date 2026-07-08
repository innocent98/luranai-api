"""API exposes scan_quality on public + dashboard auditor responses."""

from app.db.models.audit import AuditReport, AuditScope, AuditStatus, ScanQuality


def test_public_report_includes_scan_quality(client, db):
    r = AuditReport(
        tenant_id=None, url="https://ex.com", domain="ex.com",
        scope=AuditScope.PUBLIC, status=AuditStatus.FAILED,
        scan_quality=ScanQuality.UNREACHABLE,
        scan_quality_reason="We couldn't reach your website.",
        pages_scanned=0, gaps=[],
    )
    db.add(r)
    db.commit()

    resp = client.get(f"/api/v1/public/audit/{r.id}")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["scan_quality"] == "unreachable"
    assert data["scan_quality_reason"] == "We couldn't reach your website."
