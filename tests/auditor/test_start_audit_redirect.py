import pytest
from unittest.mock import patch

from app.auditor.service import AuditService, RedirectConfirmationRequired
from app.auditor.discovery import ResolvedRoot
from app.db.models.audit import AuditScope, AuditReport


def _resolved(final, changed):
    async def _fake(url, **kwargs):
        return ResolvedRoot(final_url=final, hops=(url, final), changed=changed)
    return _fake


@pytest.mark.asyncio
async def test_apex_to_www_scans_canonical_and_records_entered(db_session):
    svc = AuditService(db_session)
    with patch("app.auditor.service.resolve_and_validate_host", return_value="93.184.216.34"), \
         patch("app.auditor.service.resolve_redirect_chain",
               new=_resolved("https://www.luranai.com/", True)), \
         patch("app.auditor.service.enqueue_audit", new=lambda _id: _noop()):
        report = await svc.start_audit(
            url="https://luranai.com", scope=AuditScope.PUBLIC, tenant_id=None
        )
    fresh = db_session.get(AuditReport, report.id)
    assert fresh.url == "https://www.luranai.com/"
    # entered_url stores the NORMALIZED entered URL (normalize_url adds the
    # trailing slash for an empty path), not the raw caller-supplied string.
    assert fresh.entered_url == "https://luranai.com/"


@pytest.mark.asyncio
async def test_different_host_requires_confirmation(db_session):
    svc = AuditService(db_session)
    with patch("app.auditor.service.resolve_and_validate_host", return_value="93.184.216.34"), \
         patch("app.auditor.service.resolve_redirect_chain",
               new=_resolved("https://newco.io/", True)):
        with pytest.raises(RedirectConfirmationRequired) as ei:
            await svc.start_audit(
                url="https://oldco.com", scope=AuditScope.PUBLIC, tenant_id=None
            )
    assert ei.value.target == "https://newco.io/"
    # No report row was created.
    assert db_session.query(AuditReport).count() == 0


@pytest.mark.asyncio
async def test_matching_confirmed_target_proceeds(db_session):
    svc = AuditService(db_session)
    with patch("app.auditor.service.resolve_and_validate_host", return_value="93.184.216.34"), \
         patch("app.auditor.service.resolve_redirect_chain",
               new=_resolved("https://newco.io/", True)), \
         patch("app.auditor.service.enqueue_audit", new=lambda _id: _noop()):
        report = await svc.start_audit(
            url="https://oldco.com", scope=AuditScope.PUBLIC, tenant_id=None,
            confirmed_target="https://newco.io/",
        )
    assert db_session.get(AuditReport, report.id).url == "https://newco.io/"


@pytest.mark.asyncio
async def test_stale_confirmed_target_reprompts(db_session):
    svc = AuditService(db_session)
    with patch("app.auditor.service.resolve_and_validate_host", return_value="93.184.216.34"), \
         patch("app.auditor.service.resolve_redirect_chain",
               new=_resolved("https://newco.io/", True)):
        with pytest.raises(RedirectConfirmationRequired):
            await svc.start_audit(
                url="https://oldco.com", scope=AuditScope.PUBLIC, tenant_id=None,
                confirmed_target="https://stale-target.example/",
            )


async def _noop():
    return None
