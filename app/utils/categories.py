"""Helpers for normalizing the Tenant.category field at the response edge.

Tenants store `category` as a free-form VARCHAR in the database — the column intentionally
lives without a Postgres ENUM so the taxonomy can evolve without `ALTER TYPE` migrations.
At the API boundary we expose `TenantCategory` (a Pydantic enum). Older rows or rows
written when the taxonomy contained values we have since dropped should not break public
listings; instead we coerce unknown / None values to None so the response simply omits the
field rather than 500-ing.

This mirrors `app/utils/attribution.py::coerce_source` for ConversationSource.
"""

from __future__ import annotations

from app.schemas.explore import TenantCategory


def coerce_tenant_category(value: str | None) -> TenantCategory | None:
    """Map a raw category string to a known TenantCategory, or None if unknown / missing.

    Unknown values are silently dropped — surfacing a directory listing must not fail just
    because a tenant's stored category isn't in the current enum. Drift can be detected
    by querying the DB directly for the set of distinct categories.
    """
    if value is None:
        return None
    try:
        return TenantCategory(value)
    except ValueError:
        return None
