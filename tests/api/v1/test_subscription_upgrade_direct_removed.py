"""Regression: the deprecated direct-upgrade route must be gone, not just re-gated.

`POST /api/v1/subscription/upgrade` let any tenant admin hand themselves any
subscription tier for free (no payment processing) via
`SubscriptionService.upgrade_subscription` ->
`_update_capabilities_for_tier`. Once plan configs start carrying voice keys,
that same path becomes a privilege-escalation route to arbitrary capabilities.

The fix is deletion, not re-gating: `SubscriptionUpgradeRequest` has no
`tenant_id` field and the handler resolves the tenant from
`current_user.tenant_id`, so swapping the dependency to a platform-admin
check would not work anyway (platform admins have no tenant).

This test asserts the route itself is gone (404), not merely that some
identity is rejected (401/403) -- a 401/403 would mean the endpoint is still
live and merely unreachable without credentials, which does not close the
escalation path for anyone who does authenticate as a tenant admin.

A bare 404 on its own can't distinguish "this specific route was deleted"
from "the whole subscription router failed to mount" (a config/import
error would 404 every route under the prefix, including the ones that must
still work). The second assertion checks a sibling public route
(`/subscription/plans`, no auth required) still resolves, proving the
router itself is alive and the 404 above is specific to the deleted path.
"""

from fastapi.testclient import TestClient


def test_upgrade_direct_route_returns_404(client: TestClient):
    response = client.post(
        "/api/v1/subscription/upgrade",
        json={"tier": "professional", "billing_cycle": "monthly"},
    )

    assert response.status_code == 404

    # Prove the router is actually mounted -- a sibling public route on the
    # same router must still resolve, so the 404 above means "this route was
    # deleted," not "the whole subscription router is missing."
    assert client.get("/api/v1/subscription/plans").status_code != 404
