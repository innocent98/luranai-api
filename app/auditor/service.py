"""Auditor service: start audits (public + dashboard), import, claim, purge."""

from __future__ import annotations

import asyncio
import secrets
from typing import TYPE_CHECKING
from urllib.parse import urlparse
from uuid import UUID

from loguru import logger
from sqlalchemy.orm import Session

from app.auditor.discovery import classify_redirect, normalize_url, resolve_redirect_chain
from app.db.models.audit import AuditReport, AuditScope, AuditStatus
from app.db.models.job import JobType, ProcessingJob
from app.utils.network_security import HostResolutionError, resolve_and_validate_host

if TYPE_CHECKING:
    from app.db.models.audit import AuditEntry


class RedirectConfirmationRequired(Exception):
    """The entered URL redirects to a different host that the user must confirm
    before we scan it. Carries the entered URL and the resolved target."""

    def __init__(self, entered: str, target: str) -> None:
        self.entered = entered
        self.target = target
        super().__init__(f"{entered} redirects to {target}")


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

    @staticmethod
    async def _assert_target_auditable(normalized: str, domain: str) -> None:
        """Reject a target we already know the crawler cannot audit.

        The crawl guard would reject these anyway, but only after a report and
        job exist and a worker has picked the job up ~40s later — and it lands
        as RENDER_FAILED, which tells the user something broke on our end when
        their domain simply does not exist. Checking here turns a typo into an
        immediate, accurate error and leaves no orphan rows behind.

        This is a pre-flight check for the user's benefit, NOT the security
        boundary: DNS can change between here and the crawl, so crawler and
        discovery still guard every fetch and pin the resolved IP.

        Raises ValueError (both endpoints map it to 400 INVALID_URL).
        """
        hostname = urlparse(normalized).hostname or ""
        try:
            # getaddrinfo blocks; keep it off the event loop.
            await asyncio.to_thread(resolve_and_validate_host, hostname)
        except HostResolutionError as exc:
            logger.info("Rejected audit target {} — does not resolve: {}", domain, exc)
            raise ValueError(
                f"We couldn't find {domain}. Check the address and try again."
            ) from exc
        except ValueError as exc:
            # Resolves, but somewhere we refuse to send a crawler (private or
            # reserved space). Say no without echoing the address back.
            logger.warning("Rejected unsafe audit target {}: {}", domain, exc)
            raise ValueError(f"{domain} can't be audited.") from exc

    async def start_audit(
        self,
        *,
        url: str,
        scope: AuditScope,
        tenant_id: UUID | None,
        created_by: UUID | None = None,
        confirmed_target: str | None = None,
    ) -> AuditReport:
        normalized = normalize_url(url)
        if not normalized:
            raise ValueError("Invalid URL")
        domain = urlparse(normalized).netloc

        await self._assert_target_auditable(normalized, domain)

        # Canonicalize host redirects (apex→www, scheme upgrade) and gate a
        # genuinely different host behind explicit confirmation.
        # resolve_redirect_chain re-guards every hop and raises ValueError on
        # unsafe/loop targets.
        resolved = await resolve_redirect_chain(normalized)
        final_url = normalize_url(resolved.final_url) or resolved.final_url
        if classify_redirect(normalized, final_url) == "different_host":
            if confirmed_target is None or normalize_url(confirmed_target) != final_url:
                raise RedirectConfirmationRequired(entered=normalized, target=final_url)

        final_domain = urlparse(final_url).netloc
        entered_url = normalized if final_url != normalized else None

        job = ProcessingJob(
            tenant_id=tenant_id,  # None for public (pre-signup) audits; column is nullable
            job_type=JobType.WEBSITE_AUDIT,
            input_data={"kind": "website_audit", "url": final_url},
            created_by=created_by,
        )
        self.db.add(job)
        self.db.flush()

        report = AuditReport(
            tenant_id=tenant_id,
            url=final_url,
            domain=final_domain,
            entered_url=entered_url,
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

    def _existing_source_count(self, tenant_id: UUID) -> int:
        """Count KnowledgeSource rows already consuming the tenant's source budget.

        Audit-imported findings materialize into the SAME KnowledgeSource table as
        uploaded files/URLs, so they share the single ``max_knowledge_sources`` cap.
        ACTIVE and PROCESSING rows both count (PROCESSING = an in-flight import that
        will become ACTIVE); INACTIVE and FAILED do not.
        """
        from app.db.models.knowledge import KnowledgeSource, SourceStatus

        return (
            self.db.query(KnowledgeSource)
            .filter(
                KnowledgeSource.tenant_id == tenant_id,
                KnowledgeSource.status.in_([SourceStatus.ACTIVE, SourceStatus.PROCESSING]),
            )
            .count()
        )

    def _source_usage(self, tenant_id: UUID, audit_report_id: UUID) -> int:
        """Total source-budget consumed for staging decisions on this report.

        = materialized KnowledgeSource rows (files + already-imported findings)
          + staged ACTIVE audit entries in this report not yet imported.

        This is the tenant-global figure the plan cap is compared against, so a
        tenant who filled ``max_knowledge_sources`` with uploaded files has no room
        left to activate or import audit findings.
        """
        from app.db.models.audit import AuditEntry, AuditEntryStatus

        staged = (
            self.db.query(AuditEntry)
            .filter(
                AuditEntry.audit_report_id == audit_report_id,
                AuditEntry.status == AuditEntryStatus.ACTIVE,
                AuditEntry.knowledge_source_id.is_(None),
            )
            .count()
        )
        return self._existing_source_count(tenant_id) + staged

    def preselect_entries(self, audit_report_id: UUID) -> tuple[list[AuditEntry], list[AuditEntry]]:
        """Initialization-only preselection: assign ACTIVE/LOCKED only to PROPOSED entries.

        N is the tenant's REMAINING source budget: max_knowledge_sources minus the
        sources already consumed tenant-wide (uploaded files/URLs + already-imported
        findings + this report's staged ACTIVE entries). Audit findings share the
        single max_knowledge_sources cap with uploaded knowledge.
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

        # Slots already consumed = tenant-global source usage (uploaded files +
        # already-imported findings + this report's staged ACTIVE entries).
        already_consumed = self._source_usage(report.tenant_id, audit_report_id)

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
        remaining_slots = max(0, limit - already_consumed) if limit is not None else None
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
        limit = self._max_sources(report.tenant_id)
        # Remaining tenant-global budget: max_knowledge_sources minus sources
        # already materialized (uploaded files/URLs + prior imports). Audit findings
        # share the single max_knowledge_sources cap, so a tenant who filled it with
        # uploaded files imports nothing here. Dedup-links (below) create no new row
        # and therefore do not draw down this budget.
        budget = (
            None if limit is None else max(0, limit - self._existing_source_count(report.tenant_id))
        )
        active = (
            self.db.query(AuditEntry)
            .filter(
                AuditEntry.audit_report_id == audit_report_id,
                AuditEntry.status == AuditEntryStatus.ACTIVE,
            )
            .order_by(AuditEntry.importance_rank.asc())
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

            # No budget left for a NEW source — leave the entry ACTIVE (staged) so a
            # later plan upgrade can import it. Keep scanning: subsequent entries may
            # still dedup-link (no new row) within the same call.
            if budget is not None and budget <= 0:
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
            if budget is not None:
                budget -= 1

            job = ProcessingJob(
                tenant_id=report.tenant_id,
                job_type=JobType.WEBSITE_AUDIT,
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
        # Newly-eligible slots = remaining tenant-global budget after existing
        # sources and this report's already-staged ACTIVE entries.
        return max(0, limit - self._source_usage(report.tenant_id, audit_report_id))

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
        from app.db.models.audit import AuditEntry, AuditReport, AuditScope
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

        # Re-parent the report and all its entries. Promote to DASHBOARD scope:
        # it is now a tenant-owned report, and the post-claim re-crawl should use
        # the full dashboard page budget (not the 10-page public teaser) — PRD 4.5
        # "full scan resumes on sign-up".
        report.tenant_id = tenant_id
        report.scope = AuditScope.DASHBOARD
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


def reap_stalled_audits_sync(db, older_than_minutes: int | None = None) -> int:
    """Fail audit reports stuck in a non-terminal state past the stall window.

    A worker killed mid-crawl (e.g. OOM/SIGKILL) never runs run_audit's ``except``
    block, so its report is stranded in QUEUED/CRAWLING/ANALYZING forever and the UI
    shows an eternal "Scanning" spinner. This reaper is the safety net: it marks such
    reports FAILED. The window (AUDITOR_STALL_REAP_MINUTES, default 45) sits above any
    real crawl but below ARQ's 1h job_timeout; if a genuinely slow job later commits a
    terminal state, that simply overwrites the FAILED, so reaping early is harmless.

    Returns the count of reports reaped.
    """
    from datetime import UTC, datetime, timedelta

    from app.core.config import settings
    from app.db.models.audit import AuditReport, AuditStatus, ScanQuality

    minutes = (
        older_than_minutes
        if older_than_minutes is not None
        else settings.AUDITOR_STALL_REAP_MINUTES
    )
    cutoff = datetime.now(UTC) - timedelta(minutes=minutes)
    stalled = (
        db.query(AuditReport)
        .filter(
            AuditReport.status.in_(
                [AuditStatus.QUEUED, AuditStatus.CRAWLING, AuditStatus.ANALYZING]
            ),
            AuditReport.created_at < cutoff,
        )
        .all()
    )
    count = len(stalled)
    for report in stalled:
        report.status = AuditStatus.FAILED
        report.scan_quality = ScanQuality.RENDER_FAILED
        report.scan_quality_reason = (
            "This scan didn't finish (it was interrupted). Please try again."
        )
    db.commit()
    return count


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
