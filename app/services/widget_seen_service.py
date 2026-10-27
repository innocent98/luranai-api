"""Record that a tenant's embedded widget loaded on a customer's site.

Privacy: only the request HOSTNAME is stored (no scheme, port, path, query, IP or user agent),
taken from the browser-supplied Origin (falling back to Referer) of the public config fetch.
A request with neither header is not a browser loading the widget, so it records nothing.

Cost: the public config endpoint is hit on every page view of every customer site, so a write
happens at most once per `WIDGET_SEEN_THROTTLE` per tenant. The throttle is enforced by the
UPDATE's own WHERE clause, so concurrent requests cannot double-write.
"""

import re
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

from loguru import logger
from sqlalchemy import func, or_, update
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models.tenant import Tenant

WIDGET_SEEN_THROTTLE = timedelta(minutes=10)

_HOSTNAME = re.compile(
    r"^(?=.{1,253}$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$"
)


def extract_hostname(*raw_urls: str | None) -> str | None:
    """First usable hostname among the given URLs (Origin, then Referer), lowercased."""
    for raw in raw_urls:
        if not raw:
            continue
        try:
            host = urlparse(raw.strip()).hostname
        except ValueError:
            continue
        if not host:
            continue
        host = host.rstrip(".").lower()
        if _HOSTNAME.match(host):
            return host
    return None


def is_ignored_host(host: str) -> bool:
    for entry in settings.get_widget_seen_ignored_hosts():
        if entry.startswith("*."):
            if host.endswith(entry[1:]):
                return True
        elif host == entry:
            return True
    return False


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def record_widget_seen(
    db: Session,
    tenant: Tenant,
    origin: str | None,
    referer: str | None,
    now: datetime | None = None,
) -> bool:
    """
    Stamp first/last-seen and the host on `tenant`. Best effort: never raises.

    Returns:
        True if a write happened.
    """
    try:
        host = extract_hostname(origin, referer)
        if host is None or is_ignored_host(host):
            return False

        now = now or datetime.now(UTC)
        cutoff = now - WIDGET_SEEN_THROTTLE

        # Cheap check on the row we already loaded: skips the UPDATE for almost every request.
        last = tenant.widget_last_seen_at
        if last is not None and _aware(last) > cutoff:
            return False

        result = db.execute(
            update(Tenant)
            .where(
                Tenant.id == tenant.id,
                or_(Tenant.widget_last_seen_at.is_(None), Tenant.widget_last_seen_at <= cutoff),
            )
            .values(
                widget_last_seen_at=now,
                widget_last_seen_host=host,
                widget_first_seen_at=func.coalesce(Tenant.widget_first_seen_at, now),
            )
            .execution_options(synchronize_session=False)
        )
        db.commit()
        return result.rowcount == 1
    except Exception as e:  # noqa: BLE001 - tracking must never break the widget
        logger.warning(f"Could not record widget-seen for tenant {tenant.id}: {e}")
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return False
