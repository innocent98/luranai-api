"""Screen-recording gate (spec SR-D3): plan entitlement AND tenant preference.

Fails closed: a missing tenant or any error means off. The plan decides whether the tenant may use
recordings at all (Trial, Professional and up; not Lite); the tenant's widget setting decides
whether it is switched on. Every recording path (sign, register, widget config, send) asks here.
"""

from loguru import logger
from sqlalchemy.orm import Session

from app.db.models.tenant import Tenant
from app.services.entitlements import get_entitlements


def screen_recording_allowed(db: Session, tenant: Tenant | None) -> bool:
    try:
        if tenant is None or not tenant.widget_allow_screen_recording:
            return False
        return get_entitlements(tenant, db).enabled("screen_recording")
    except Exception:
        logger.exception("screen_recording_allowed failed; failing closed")
        return False
