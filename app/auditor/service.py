"""Auditor service: start audits (public + dashboard), import, claim, purge."""

from __future__ import annotations

import secrets
from urllib.parse import urlparse
from uuid import UUID

from loguru import logger
from sqlalchemy.orm import Session

from app.auditor.discovery import normalize_url
from app.db.models.audit import AuditReport, AuditScope, AuditStatus
from app.db.models.job import JobType, ProcessingJob


def enqueue_audit(report_id: UUID) -> None:
    """Enqueue the ARQ job. Separated for test mockability."""
    import asyncio

    from arq import create_pool
    from arq.connections import RedisSettings

    from app.core.config import settings

    async def _enqueue() -> None:
        redis = await create_pool(RedisSettings.from_dsn(settings.REDIS_URL))
        await redis.enqueue_job("run_website_audit", str(report_id))

    try:
        asyncio.get_running_loop()
        asyncio.ensure_future(_enqueue())  # already in loop (endpoint context)
    except RuntimeError:
        asyncio.run(_enqueue())


class AuditService:
    def __init__(self, db: Session):
        self.db = db

    def start_audit(
        self, *, url: str, scope: AuditScope, tenant_id: UUID | None, created_by: UUID | None = None
    ) -> AuditReport:
        normalized = normalize_url(url)
        if not normalized:
            raise ValueError("Invalid URL")
        domain = urlparse(normalized).netloc

        job = ProcessingJob(
            tenant_id=tenant_id,  # None for public (pre-signup) audits; column is nullable
            job_type=JobType.DOCUMENT_INGESTION,
            input_data={"kind": "website_audit", "url": normalized},
            created_by=created_by,
        )
        self.db.add(job)
        self.db.flush()

        report = AuditReport(
            tenant_id=tenant_id,
            url=normalized,
            domain=domain,
            scope=scope,
            status=AuditStatus.QUEUED,
            processing_job_id=job.id,
            claim_token=secrets.token_urlsafe(24) if scope == AuditScope.PUBLIC else None,
        )
        self.db.add(report)
        self.db.flush()
        self.db.commit()
        logger.info("Started {} audit {} for {}", scope.value, report.id, domain)
        enqueue_audit(report.id)
        return report
