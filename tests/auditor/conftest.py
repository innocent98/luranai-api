"""Shared fixtures for the auditor test package."""

from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _resolvable_audit_target():
    """Stub the submit-time target check so auditor tests never touch DNS.

    start_audit resolves the target host before queueing work, so without this
    every test that starts an audit would depend on whether example.com (or
    example0.com, or ex.com) resolves from the machine running the suite —
    making the suite fail on a plane and pass at a desk.

    Tests that exercise rejection re-patch this target with a side_effect; an
    inner patch wins over this one. Only the SERVICE-level check is stubbed —
    discovery/crawler keep their own guards, so tests of the crawl path still
    exercise real guard logic.
    """
    with patch("app.auditor.service.resolve_and_validate_host", return_value="93.184.216.34"):
        yield
