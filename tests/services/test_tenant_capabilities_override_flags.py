"""`TenantService.update_capabilities` must record explicit manual overrides.

`override_flags` is the guard `SubscriptionService._update_capabilities_for_tier`
reads to decide which fields survive a tier sync (see
`app/services/subscription_service.py`). Before this fix, `update_capabilities`
never wrote to `override_flags` at all -- the column stayed `None`/`{}` no
matter what a platform admin set, so the "manual overrides are preserved"
promise in `_update_capabilities_for_tier`'s docstring was inert.

Every assertion here re-queries the row after `db_session.expire_all()`,
never trusting the in-memory object the service call returns -- an in-place
mutation of a JSON column can look correct in memory while never reaching
the database (SQLAlchemy does not track dict mutation on a JSON column by
default).
"""

from app.db.models.tenant import TenantCapabilities
from app.schemas.tenant import TenantCapabilitiesUpdate
from app.services.tenant_service import TenantService
from tests.conftest import _bare_voice_tenant as _tenant


def _tenant_with_trial_capabilities(db_session):
    tenant = _tenant(db_session)
    db_session.add(
        TenantCapabilities(
            tenant_id=tenant.id,
            can_access_analytics=False,
            max_knowledge_sources=2,
            override_flags=None,  # exercise the nullable column explicitly
        )
    )
    db_session.commit()
    db_session.refresh(tenant)
    return tenant


def _fetch_capabilities(db_session, tenant_id):
    db_session.expire_all()
    return (
        db_session.query(TenantCapabilities).filter(TenantCapabilities.tenant_id == tenant_id).one()
    )


def test_update_capabilities_flags_exactly_the_explicitly_set_fields(db_session):
    """Would pass a no-op implementation only if it wrote nothing -- the `==`
    (not `.items() <=`) catches both under- and over-flagging: a fix that
    flags every field on the schema (not just the ones sent) fails this too,
    since `can_upload_knowledge` was never mentioned in the request."""
    tenant = _tenant_with_trial_capabilities(db_session)

    TenantService(db_session).update_capabilities(
        tenant.id,
        TenantCapabilitiesUpdate(max_knowledge_sources=999, can_access_analytics=True),
    )

    caps = _fetch_capabilities(db_session, tenant.id)
    assert caps.max_knowledge_sources == 999
    assert caps.can_access_analytics is True
    assert caps.override_flags == {
        "max_knowledge_sources": True,
        "can_access_analytics": True,
    }


def test_update_capabilities_merges_overrides_across_separate_calls(db_session):
    """A second admin action must not wipe the first one's flags. Would fail
    if the write assigned a fresh dict built only from the current call's
    fields instead of merging onto the existing override_flags."""
    tenant = _tenant_with_trial_capabilities(db_session)
    service = TenantService(db_session)

    service.update_capabilities(tenant.id, TenantCapabilitiesUpdate(max_knowledge_sources=999))
    service.update_capabilities(tenant.id, TenantCapabilitiesUpdate(can_access_analytics=True))

    caps = _fetch_capabilities(db_session, tenant.id)
    assert caps.override_flags == {
        "max_knowledge_sources": True,
        "can_access_analytics": True,
    }


def test_update_capabilities_with_no_fields_set_does_not_touch_override_flags(db_session):
    """An empty update (nothing set on the request) must not flag anything --
    guards against a naive implementation that flags every field declared on
    `TenantCapabilitiesUpdate` rather than only the ones actually sent."""
    tenant = _tenant_with_trial_capabilities(db_session)

    TenantService(db_session).update_capabilities(tenant.id, TenantCapabilitiesUpdate())

    caps = _fetch_capabilities(db_session, tenant.id)
    assert not caps.override_flags
