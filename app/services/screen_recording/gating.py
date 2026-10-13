"""Screen-recording gate (spec SR-D3): platform capability AND tenant preference.

Fails closed: a missing capabilities row, a missing tenant or any error means off.
"""

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.tenant import Tenant, TenantCapabilities


def screen_recording_allowed(db: Session, tenant: Tenant | None) -> bool:
    try:
        if tenant is None or not tenant.widget_allow_screen_recording:
            return False
        caps = db.execute(
            select(TenantCapabilities).where(TenantCapabilities.tenant_id == tenant.id)
        ).scalar_one_or_none()
        return bool(caps and caps.can_use_screen_recording)
    except Exception:
        logger.exception("screen_recording_allowed failed; failing closed")
        return False
