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


async def enqueue_audit(report_id: UUID) -> None:
    """Enqueue the ARQ job for a website audit. Raises on failure — caller must handle."""
    from arq import create_pool
    from arq.connections import RedisSettings

    from app.core.config import settings

    redis = await create_pool(RedisSettings.from_dsn(settings.REDIS_URL))
    await redis.enqueue_job("run_website_audit", str(report_id))


class AuditService:
    def __init__(self, db: Session):
        self.db = db

    async def start_audit(
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

        try:
            await enqueue_audit(report.id)
        except Exception as exc:
            logger.error("Failed to enqueue audit {}: {}", report.id, exc)
            # Mark both the report and its processing job as FAILED so no row is
            # left stuck in QUEUED with no worker behind it.
            report.status = AuditStatus.FAILED
            job.status = "failed"  # type: ignore[assignment]
            self.db.commit()
            raise RuntimeError(f"Audit enqueue failed: {exc}") from exc

        return report

    # ------------------------------------------------------------------
    # Import-entries: plan-gating + materialization into RAG knowledge base
    # ------------------------------------------------------------------

    def _max_sources(self, tenant_id: UUID) -> int | None:
        from app.db.models.tenant import TenantCapabilities

        cap = (
            self.db.query(TenantCapabilities)
            .filter(TenantCapabilities.tenant_id == tenant_id)
            .one_or_none()
        )
        return cap.max_knowledge_sources if cap else 0

    def preselect_entries(
        self, audit_report_id: UUID
    ) -> tuple[list["AuditEntry"], list["AuditEntry"]]:
        """Initialization-only preselection: assign ACTIVE/LOCKED only to PROPOSED entries.

        N is determined by TenantCapabilities.max_knowledge_sources.
        None = unlimited; missing capabilities row = 0 (no sources allowed).

        Idempotency guarantee: entries already in ACTIVE, LOCKED, or IMPORTED status
        are left untouched, preserving any manual swaps the tenant has made via PATCH.
        Only entries still in PROPOSED status are eligible for initial assignment.
        The top-N PROPOSED entries (by importance_rank) become ACTIVE; the rest become LOCKED.
        The plan-limit cap on the import side is enforced by import_entries, which only
        processes entries currently in ACTIVE status — so an over-limit active set
        (impossible via PATCH if that endpoint is correctly guarded) cannot bypass the cap.
        """
        from app.db.models.audit import AuditEntry, AuditEntryStatus, AuditReport

        report = self.db.query(AuditReport).filter(AuditReport.id == audit_report_id).one()
        limit = self._max_sources(report.tenant_id)

        # Count how many slots are already consumed by tenant-curated ACTIVE entries
        already_active_count = (
            self.db.query(AuditEntry)
            .filter(
                AuditEntry.audit_report_id == audit_report_id,
                AuditEntry.status == AuditEntryStatus.ACTIVE,
            )
            .count()
        )

        # Only operate on entries still in PROPOSED — never touch ACTIVE/LOCKED/IMPORTED
        proposed = (
            self.db.query(AuditEntry)
            .filter(
                AuditEntry.audit_report_id == audit_report_id,
                AuditEntry.status == AuditEntryStatus.PROPOSED,
            )
            .order_by(AuditEntry.importance_rank.asc())
            .all()
        )

        active: list = []
        locked: list = []
        remaining_slots = (limit - already_active_count) if limit is not None else None
        for entry in proposed:
            if remaining_slots is None or remaining_slots > 0:
                entry.status = AuditEntryStatus.ACTIVE
                active.append(entry)
                if remaining_slots is not None:
                    remaining_slots -= 1
            else:
                entry.status = AuditEntryStatus.LOCKED
                locked.append(entry)
        self.db.flush()
        return active, locked

    async def import_entries(
        self, audit_report_id: UUID, *, uploaded_by: UUID | None = None
    ) -> list[UUID]:
        """Materialize ACTIVE AuditEntry rows into KnowledgeSource + enqueue ingestion.

        Idempotent: entries already IMPORTED (knowledge_source_id set) are skipped.
        Dedup: entries whose content_hash matches an existing KnowledgeSource are
        linked to the existing source rather than creating a duplicate.

        Now async so that enqueue failures surface immediately (no orphaned
        KnowledgeSource rows left stuck at PROCESSING).
        """
        from datetime import UTC, datetime

        from app.auditor.classify import map_kind
        from app.db.models.audit import AuditEntry, AuditEntryStatus, AuditReport
        from app.db.models.knowledge import KnowledgeSource, SourceStatus

        report = self.db.query(AuditReport).filter(AuditReport.id == audit_report_id).one()
        active = (
            self.db.query(AuditEntry)
            .filter(
                AuditEntry.audit_report_id == audit_report_id,
                AuditEntry.status == AuditEntryStatus.ACTIVE,
            )
            .all()
        )
        imported_ids: list[UUID] = []
        for entry in active:
            # Idempotency: already imported in a prior call — skip entirely
            if entry.knowledge_source_id is not None:
                continue

            # Re-scan dedup: an existing source with the same content_hash
            dup = (
                self.db.query(KnowledgeSource)
                .filter(
                    KnowledgeSource.tenant_id == report.tenant_id,
                    KnowledgeSource.content_hash == entry.content_hash,
                    KnowledgeSource.status != SourceStatus.INACTIVE,
                )
                .one_or_none()
            )
            if dup is not None:
                entry.status = AuditEntryStatus.IMPORTED
                entry.knowledge_source_id = dup.id
                continue

            source = KnowledgeSource(
                tenant_id=report.tenant_id,
                name=entry.title or entry.url,
                source_type=map_kind(entry.kind),
                source_category="url",
                source_url=entry.url,
                content_hash=entry.content_hash,
                version=1,
                status=SourceStatus.PROCESSING,
                meta={"auditor_category": entry.category, "audit_entry_id": str(entry.id)},
                uploaded_by=uploaded_by,
            )
            self.db.add(source)
            self.db.flush()
            entry.status = AuditEntryStatus.IMPORTED
            entry.knowledge_source_id = source.id

            job = ProcessingJob(
                tenant_id=report.tenant_id,
                job_type=JobType.DOCUMENT_INGESTION,
                input_data={
                    "tenant_id": str(report.tenant_id),
                    "document_text": entry.content,
                    "document_name": entry.title or entry.url,
                    "source_type": map_kind(entry.kind).value,
                    "source_category": "url",
                    "source_url": entry.url,
                    "metadata": {"auditor_category": entry.category},
                    # Pass the pre-created source so the worker fills THIS source
                    # rather than creating a second orphan KnowledgeSource.
                    "knowledge_source_id": str(source.id),
                },
                created_by=uploaded_by,
            )
            self.db.add(job)
            self.db.flush()
            imported_ids.append(entry.id)
            await enqueue_document_ingestion(job.id)

        report.imported = True
        report.imported_at = datetime.now(UTC)
        self.db.commit()
        return imported_ids

    def unlock_after_upgrade(self, audit_report_id: UUID) -> int:
        """Return count of LOCKED entries that become eligible after a plan upgrade.

        Does NOT automatically flip entries to ACTIVE — just returns the count so
        the caller can decide whether to re-run preselect_entries.
        """
        from app.db.models.audit import AuditEntry, AuditEntryStatus, AuditReport

        report = self.db.query(AuditReport).filter(AuditReport.id == audit_report_id).one()
        limit = self._max_sources(report.tenant_id)
        if limit is None:
            # Unlimited: all locked entries become eligible
            return (
                self.db.query(AuditEntry)
                .filter(
                    AuditEntry.audit_report_id == audit_report_id,
                    AuditEntry.status == AuditEntryStatus.LOCKED,
                )
                .count()
            )
        active_count = (
            self.db.query(AuditEntry)
            .filter(
                AuditEntry.audit_report_id == audit_report_id,
                AuditEntry.status == AuditEntryStatus.ACTIVE,
            )
            .count()
        )
        return max(0, limit - active_count)


    async def claim_audit(self, claim_token: str, tenant_id: UUID) -> AuditReport:
        """Re-parent a public audit to a tenant after sign-up.

        - Looks up the unclaimed AuditReport by claim_token.
        - Re-parents the report and its AuditEntry rows to tenant_id.
        - Persists the BusinessProfile for the tenant if present.
        - If the report is within the staleness window: incremental resume (re-enqueue
          with resume=True). Otherwise: fresh full crawl (re-enqueue with resume=False).
        - Raises ValueError if the token is unknown or already claimed.
        """
        from datetime import UTC, datetime, timedelta

        from app.core.config import settings
        from app.db.models.audit import AuditEntry, AuditReport
        from app.db.models.business_profile import BusinessProfile
        from app.db.models.job import JobType, ProcessingJob
        from app.utils.datetime import ensure_utc_aware

        report = (
            self.db.query(AuditReport)
            .filter(AuditReport.claim_token == claim_token, AuditReport.tenant_id.is_(None))
            .one_or_none()
        )
        if report is None:
            raise ValueError("Invalid or already-claimed token")

        # Re-parent the report and all its entries
        report.tenant_id = tenant_id
        self.db.query(AuditEntry).filter(AuditEntry.audit_report_id == report.id).update(
            {AuditEntry.tenant_id: tenant_id}
        )

        # Persist BusinessProfile ownership if one is attached
        if report.business_profile_id:
            bp = (
                self.db.query(BusinessProfile)
                .filter(BusinessProfile.id == report.business_profile_id)
                .one_or_none()
            )
            if bp and bp.tenant_id is None:
                bp.tenant_id = tenant_id

        # Staleness check: compare scan time to threshold
        created = ensure_utc_aware(report.created_at)
        stale = created is not None and (datetime.now(UTC) - created) > timedelta(
            days=settings.AUDITOR_CLAIM_STALENESS_DAYS
        )

        # Create a new processing job for the re-crawl / resume
        job = ProcessingJob(
            tenant_id=tenant_id,
            job_type=JobType.DOCUMENT_INGESTION,
            input_data={
                "kind": "website_audit",
                "resume": not stale,  # True = incremental resume; False = fresh crawl
                "url": report.url,
            },
        )
        self.db.add(job)
        self.db.flush()
        report.processing_job_id = job.id
        self.db.commit()

        # Enqueue — must not be fire-and-forget; a Redis failure must surface.
        await enqueue_audit(report.id)
        return report


def purge_unclaimed_audits_sync(db) -> int:
    """Delete AuditReport rows with tenant_id IS NULL older than the purge window.

    AuditEntry rows are cascade-deleted via the FK relationship.
    Returns the count of deleted reports.
    """
    from datetime import UTC, datetime, timedelta

    from app.core.config import settings
    from app.db.models.audit import AuditReport

    cutoff = datetime.now(UTC) - timedelta(days=settings.AUDITOR_UNCLAIMED_PURGE_DAYS)
    stale = (
        db.query(AuditReport)
        .filter(AuditReport.tenant_id.is_(None), AuditReport.created_at < cutoff)
        .all()
    )
    count = len(stale)
    for report in stale:
        db.delete(report)  # cascade deletes AuditEntry rows via relationship
    db.commit()
    return count


async def enqueue_document_ingestion(job_id: UUID) -> None:
    """Enqueue the document_processor ARQ job. Separated for test mockability.

    Now async so callers can await it and surface Redis failures immediately.
    """
    from arq import create_pool
    from arq.connections import RedisSettings

    from app.core.config import settings

    redis = await create_pool(RedisSettings.from_dsn(settings.REDIS_URL))
    await redis.enqueue_job("process_document_ingestion", str(job_id))
