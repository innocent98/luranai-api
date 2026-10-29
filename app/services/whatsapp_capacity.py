"""WhatsApp capacity alert: tell a platform operator BEFORE the VPS fills up.

Two independent conditions, each emailed at most once per `DEDUPE_SECONDS` (a Redis `SET NX EX`
decides which process sends):

* `connections`: live linked sessions reached `WHATSAPP_CAPACITY_ALERT_PERCENT` of
  `WHATSAPP_MAX_CONNECTIONS` (the backend guard that answers tenants 503 `capacity_exceeded`).
* `sidecar_memory`: the sidecar's RSS reached the same percent of its own refuse threshold
  (only known from the sidecar's `/health`, so only the cron path checks it).

Nothing here may break a request or a worker job: `check_and_alert` and
`schedule_post_create_check` never raise. Log lines carry exception classes and counts only.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import cast

import redis.asyncio as aioredis
from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.channels.base import SidecarHealth
from app.channels.linked_device import LinkedDeviceAdapter
from app.channels.registry import get_adapter
from app.core.config import settings
from app.db.models.whatsapp import LIVE_STATUSES, WhatsAppConnection
from app.db.session import SessionLocal
from app.services.email_service import EmailService

CONDITIONS = ("connections", "sidecar_memory")
DEDUPE_SECONDS = 6 * 3600
KEY_PREFIX = "wa:capacity_alert:"
_REDIS_TIMEOUT_S = 2.0

# Strong references: a bare `create_task` result can be garbage-collected mid-flight.
_tasks: set[asyncio.Task] = set()


@dataclass(frozen=True)
class Alert:
    condition: str
    detail_lines: list[str] = field(default_factory=list)


def _percent() -> int:
    return settings.WHATSAPP_CAPACITY_ALERT_PERCENT


def evaluate(live_count: int, health: SidecarHealth | None) -> list[Alert]:
    """Pure: which conditions are at or past the alert threshold."""
    alerts: list[Alert] = []
    percent = _percent()

    limit = settings.WHATSAPP_MAX_CONNECTIONS
    if limit > 0 and live_count * 100 >= percent * limit:
        alerts.append(
            Alert(
                "connections",
                [
                    f"Live WhatsApp connections: {live_count} of {limit} allowed "
                    f"({live_count * 100 // limit}%).",
                    f"Alert threshold: {percent}%. New connections are refused (503) at 100%.",
                ],
            )
        )

    if health is not None and health.rss_refuse_mb > 0:
        if health.rss_mb * 100 >= percent * health.rss_refuse_mb:
            alerts.append(
                Alert(
                    "sidecar_memory",
                    [
                        f"Sidecar memory: {health.rss_mb:.0f} MB of the "
                        f"{health.rss_refuse_mb:.0f} MB refuse threshold "
                        f"({int(health.rss_mb * 100 // health.rss_refuse_mb)}%).",
                        f"Sidecar sessions: {health.sessions} of {health.max_sessions}.",
                        f"Alert threshold: {percent}%. The sidecar refuses new sessions at 100%.",
                    ],
                )
            )
    return alerts


def _recipients() -> list[str]:
    return [a.strip() for a in settings.PLATFORM_ALERT_EMAILS.split(",") if a.strip()]


def _live_count(db: Session) -> int:
    return int(
        db.execute(
            select(func.count())
            .select_from(WhatsAppConnection)
            .where(WhatsAppConnection.status.in_(LIVE_STATUSES))
        ).scalar_one()
    )


async def _send_all(recipients: list[str], alert: Alert) -> bool:
    """Blocking SMTP runs off the event loop. True when at least one recipient got it."""
    service = EmailService()
    delivered = False
    for to_email in recipients:
        try:
            ok = await asyncio.to_thread(
                service.send_whatsapp_capacity_alert_email,
                to_email,
                alert.condition,
                alert.detail_lines,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("whatsapp capacity alert send raised: {}", type(exc).__name__)
            continue
        delivered = delivered or bool(ok)
    return delivered


async def _check_and_alert(db: Session, include_health: bool) -> list[str]:
    live = _live_count(db)
    health: SidecarHealth | None = None
    if include_health:
        try:
            adapter = cast(LinkedDeviceAdapter, get_adapter("linked_device"))
            health = await adapter.get_health()
        except Exception as exc:  # noqa: BLE001
            logger.warning("whatsapp capacity: sidecar health unavailable: {}", type(exc).__name__)

    alerts = evaluate(live, health)
    if not alerts:
        return []

    # Checked BEFORE the dedupe key is taken: a missing config must not silence the alert
    # for six hours once the operator fixes it.
    recipients = _recipients()
    if not recipients:
        logger.warning(
            "whatsapp capacity alert firing ({}) but PLATFORM_ALERT_EMAILS is empty",
            ",".join(a.condition for a in alerts),
        )
        return []

    emailed: list[str] = []
    redis = aioredis.from_url(
        settings.REDIS_URL,
        socket_connect_timeout=_REDIS_TIMEOUT_S,
        socket_timeout=_REDIS_TIMEOUT_S,
    )
    try:
        for alert in alerts:
            key = f"{KEY_PREFIX}{alert.condition}"
            try:
                won = await redis.set(key, "1", nx=True, ex=DEDUPE_SECONDS)
            except Exception as exc:  # noqa: BLE001
                # Cannot dedupe, so do not send: better silent than a mail every 5 minutes.
                logger.error("whatsapp capacity dedupe unavailable: {}", type(exc).__name__)
                continue
            if not won:
                continue
            if await _send_all(recipients, alert):
                emailed.append(alert.condition)
                logger.warning("whatsapp capacity alert sent: {}", alert.condition)
                continue
            logger.error("whatsapp capacity alert undelivered: {}", alert.condition)
            try:
                await redis.delete(key)  # let the next run retry
            except Exception as exc:  # noqa: BLE001
                logger.error("whatsapp capacity dedupe release failed: {}", type(exc).__name__)
    finally:
        try:
            await redis.aclose()
        except Exception as exc:  # noqa: BLE001
            logger.debug("whatsapp capacity redis close failed: {}", type(exc).__name__)
    return emailed


async def check_and_alert(db: Session, *, include_health: bool) -> list[str]:
    """Evaluate, dedupe and email. Returns the conditions emailed. Never raises."""
    try:
        return await _check_and_alert(db, include_health)
    except Exception as exc:  # noqa: BLE001
        logger.error("whatsapp capacity check failed: {}", type(exc).__name__)
        return []


async def _post_create_check() -> None:
    try:
        db = SessionLocal()
        try:
            await check_and_alert(db, include_health=False)
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001
        logger.error("whatsapp post-create capacity check failed: {}", type(exc).__name__)


def schedule_post_create_check() -> None:
    """Fire and forget from the request path: own DB session, never raises into the caller."""
    try:
        task = asyncio.get_running_loop().create_task(_post_create_check())
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
    except Exception as exc:  # noqa: BLE001
        logger.error("whatsapp post-create capacity check not scheduled: {}", type(exc).__name__)
