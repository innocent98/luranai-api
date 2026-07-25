"""Tests for the 409 redirect-confirmation contract on POST /public/audit."""
from unittest.mock import patch

from app.auditor.discovery import ResolvedRoot
from app.auditor.service import RedirectConfirmationRequired  # noqa: F401 (documents the contract)


def _resolved(final, changed):
    async def _fake(url, **kwargs):
        return ResolvedRoot(final_url=final, hops=(url, final), changed=changed)
    return _fake


def test_different_host_redirect_returns_409(client):
    with patch("app.auditor.service.resolve_and_validate_host", return_value="93.184.216.34"), \
         patch("app.auditor.service.resolve_redirect_chain",
               new=_resolved("https://newco.io/", True)):
        resp = client.post("/api/v1/public/audit", json={"url": "https://oldco.com"})
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"]["code"] == "REDIRECT_CONFIRMATION_REQUIRED"
    assert body["error"]["details"]["redirect_target"] == "https://newco.io/"


def test_apex_to_www_returns_202(client):
    with patch("app.auditor.service.resolve_and_validate_host", return_value="93.184.216.34"), \
         patch("app.auditor.service.resolve_redirect_chain",
               new=_resolved("https://www.luranai.com/", True)), \
         patch("app.auditor.service.enqueue_audit", new=lambda _id: _n()):
        resp = client.post("/api/v1/public/audit", json={"url": "https://luranai.com"})
    assert resp.status_code == 202


async def _n():
    return None


def test_confirmation_does_not_spend_scan_credit(client):
    calls = {"acquire": 0}

    async def _count_acquire(identifier="default"):
        calls["acquire"] += 1

    async def _usage(identifier="default"):
        return {"calls": 0, "remaining": 3}

    with patch("app.auditor.service.resolve_and_validate_host", return_value="93.184.216.34"), \
         patch("app.auditor.service.resolve_redirect_chain",
               new=_resolved("https://newco.io/", True)), \
         patch("app.api.v1.endpoints.public_auditor.public_audit_limiter.acquire", new=_count_acquire), \
         patch("app.api.v1.endpoints.public_auditor.public_audit_limiter.get_current_usage", new=_usage):
        resp = client.post("/api/v1/public/audit", json={"url": "https://oldco.com"})
    assert resp.status_code == 409
    assert calls["acquire"] == 0  # no scan credit spent on a confirmation prompt
