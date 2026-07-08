"""Single multi-phase auditor pipeline + ARQ entrypoint."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from loguru import logger
from sqlalchemy.orm import Session

from app.auditor import classify, crawler, discovery, extraction, gaps, profile, scoring
from app.core.config import settings
from app.db.models.audit import (
    AuditEntry,
    AuditEntryStatus,
    AuditReport,
    AuditScope,
    AuditStatus,
)
from app.db.models.job import JobStatus, ProcessingJob


def _active_source_cap(db: Session, tenant_id) -> int | None:
    """Plan cap on active knowledge sources (None == unlimited, 0 == none).

    Mirrors AuditService._max_sources; kept here as a small seam so the re-crawl
    activation-restore can enforce the same cap and tests can patch it.
    """
    from app.db.models.tenant import TenantCapabilities

    cap = (
        db.query(TenantCapabilities).filter(TenantCapabilities.tenant_id == tenant_id).one_or_none()
    )
    return cap.max_knowledge_sources if cap else 0


def _max_pages(db: Session, report: AuditReport) -> int:
    if report.scope == AuditScope.PUBLIC:
        return settings.AUDITOR_PUBLIC_MAX_PAGES
    from app.db.models.tenant import TenantCapabilities

    cap = (
        db.query(TenantCapabilities)
        .filter(TenantCapabilities.tenant_id == report.tenant_id)
        .one_or_none()
    )
    if cap and cap.max_audit_pages_per_crawl:
        return cap.max_audit_pages_per_crawl
    return settings.AUDITOR_DASHBOARD_DEFAULT_MAX_PAGES


def _update_job(db: Session, job_id: UUID | None, **fields) -> None:
    if not job_id:
        return
    job = db.query(ProcessingJob).filter(ProcessingJob.id == job_id).one_or_none()
    if not job:
        return
    for k, v in fields.items():
        setattr(job, k, v)
    try:
        db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("_update_job commit failed: {}", exc)
        db.rollback()


async def run_audit(
    db: Session,
    audit_report_id: UUID,
    *,
    page_renderer=None,
    fetch_text=None,
    resume: bool = False,
) -> AuditReport:
    """Orchestrate all auditor phases and persist results.

    Args:
        resume: When False (default), delete any pre-existing AuditEntry rows
            for this report and perform a full crawl.  When True, load existing
            entries, skip already-seen URLs, and append only the newly discovered
            pages — used after claim_audit when the audit is still fresh.

    Catches all internal exceptions, marks the report FAILED, and returns
    the report — callers (including run_website_audit) never see a raise from
    this function.
    """
    report = db.query(AuditReport).filter(AuditReport.id == audit_report_id).one()

    try:
        # ------------------------------------------------------------------ #
        # Phase 0: mark CRAWLING                                              #
        # ------------------------------------------------------------------ #
        report.status = AuditStatus.CRAWLING
        db.commit()
        _update_job(
            db,
            report.processing_job_id,
            status=JobStatus.PROCESSING,
            started_at=datetime.now(UTC),
        )

        # ------------------------------------------------------------------ #
        # Phase 1a: resume — load existing entries, build already_have set   #
        # ------------------------------------------------------------------ #
        existing_entries: list[AuditEntry] = []
        already_have: set[str] = set()
        if resume:
            existing_entries = (
                db.query(AuditEntry).filter(AuditEntry.audit_report_id == report.id).all()
            )
            already_have = {e.url for e in existing_entries}
            # Edge case: no existing entries → fall through to fresh-run behaviour
            if not existing_entries:
                resume = False

        # Preserve the user's active-source selections across a fresh re-crawl:
        # capture which URLs were ACTIVE before wiping, then re-activate the
        # matching new entries after classification (Phase 4).
        preserved_active_urls: set[str] = set()
        if not resume:
            preserved_active_urls = {
                e.url
                for e in db.query(AuditEntry)
                .filter(
                    AuditEntry.audit_report_id == report.id,
                    AuditEntry.status == AuditEntryStatus.ACTIVE,
                )
                .all()
            }
            # Fresh run: wipe any pre-existing entries so we never duplicate
            db.query(AuditEntry).filter(AuditEntry.audit_report_id == report.id).delete()
            db.commit()
            existing_entries = []
            already_have = set()

        # ------------------------------------------------------------------ #
        # Phase 1b: seed URLs (root + sitemap.xml)                            #
        # ------------------------------------------------------------------ #
        cap = _max_pages(db, report)
        seeds = await discovery.seed_urls(report.url, cap, fetch_text=fetch_text)

        report.pages_discovered = len(already_have) + len(seeds)
        _update_job(
            db,
            report.processing_job_id,
            total_items=len(existing_entries) + len(seeds),
            processed_items=0,
        )
        db.commit()

        # ------------------------------------------------------------------ #
        # Phase 2: render-crawl (BFS from seeds) + extract                    #
        # ------------------------------------------------------------------ #
        robots = await crawler.fetch_robots(report.url, fetch_text=fetch_text)
        raw_pages = await crawler.crawl_site(
            seeds,
            cap,
            page_renderer=page_renderer,
            robots_disallowed=robots,
            skip_urls=already_have if resume else None,
        )
        # pages_discovered now reflects what the crawl actually reached.
        report.pages_discovered = len(already_have) + len(raw_pages)
        extracted = extraction.extract_pages(raw_pages)
        report.pages_scanned = len(existing_entries) + len(extracted)
        _update_job(
            db,
            report.processing_job_id,
            processed_items=len(existing_entries) + len(extracted),
        )
        db.commit()

        # ------------------------------------------------------------------ #
        # Phase 2b: classify scan outcome (honest states)                    #
        # ------------------------------------------------------------------ #
        from app.auditor.scan_quality import classify_scan_outcome, ScanUnreadable
        from app.db.models.audit import ScanQuality

        if resume:
            report.scan_quality = ScanQuality.OK
            report.scan_quality_reason = None
            db.commit()
        else:
            root_norm = discovery.normalize_url(report.url)
            root_page = next(
                (p for p in raw_pages if p.url == root_norm),
                raw_pages[0] if raw_pages else None,
            )
            quality, reason = classify_scan_outcome(
                root_page,
                extracted,
                thin_min_content_chars=settings.AUDITOR_THIN_MIN_CONTENT_CHARS,
            )
            report.scan_quality = quality
            report.scan_quality_reason = reason or None
            db.commit()  # persist BEFORE any raise so the FAILED path keeps it
            if quality in (ScanQuality.UNREACHABLE, ScanQuality.RENDER_FAILED):
                raise ScanUnreadable(quality, reason)

        # ------------------------------------------------------------------ #
        # Phase 3: business profile synthesis / reuse                         #
        # ------------------------------------------------------------------ #
        report.status = AuditStatus.ANALYZING
        db.commit()

        if report.tenant_id is not None:
            from app.db.models.business_profile import BusinessProfile

            # Load any existing profile so we can supply its stored content
            # signature.  synthesize_or_reuse reuses the profile when
            # prior_signature matches the signature of the *current* pages,
            # meaning the site content is unchanged since the last audit.
            # BusinessProfile.raw stores the signature under key
            # "_content_signature" after each synthesis (written below).
            existing_bp = (
                db.query(BusinessProfile)
                .filter(BusinessProfile.tenant_id == report.tenant_id)
                .one_or_none()
            )
            stored_sig: str | None = (
                (existing_bp.raw or {}).get("_content_signature")
                if existing_bp is not None
                else None
            )
            profile_obj, new_sig = await profile.synthesize_or_reuse(
                db, report.tenant_id, extracted, prior_signature=stored_sig
            )
            # Persist the content signature so future re-audits can reuse.
            if profile_obj.raw is None:
                profile_obj.raw = {}
            profile_obj.raw = {**profile_obj.raw, "_content_signature": new_sig}
            db.flush()
            report.business_profile_id = profile_obj.id
            profile_dict: dict = {
                "summary": profile_obj.summary,
                "industry": profile_obj.industry,
                "offerings": profile_obj.offerings or [],
                "audience": profile_obj.audience or [],
                "positioning": profile_obj.positioning,
                "key_entities": profile_obj.key_entities or [],
                "tone": profile_obj.tone or {},
                "confidence": profile_obj.confidence,
            }
        else:
            # Public audit — no tenant; synthesize in-memory, no persistence
            profile_dict = await profile.synthesize_profile(extracted)
            # No business_profile_id for public audits
        db.commit()

        # ------------------------------------------------------------------ #
        # Phase 4: classify pages (adaptive, profile-aware)                   #
        # ------------------------------------------------------------------ #
        # Seed found sets from existing entries so union recompute is correct
        found_kinds: set[str] = {e.kind.value for e in existing_entries}
        found_categories: set[str] = {e.category for e in existing_entries}

        classified = await classify.classify_pages(extracted, profile_dict)
        for page, c in classified:
            found_kinds.add(c.kind.value)
            found_categories.add(c.category)
            db.add(
                AuditEntry(
                    audit_report_id=report.id,
                    tenant_id=report.tenant_id,
                    url=page.url,
                    title=page.title,
                    category=c.category,
                    kind=c.kind,
                    content=page.text,
                    content_hash=page.content_hash,
                    importance_rank=c.importance_rank,
                    status=AuditEntryStatus.PROPOSED,
                    meta={"extraction_confidence": 1.0},
                )
            )
        db.flush()

        # Restore the user's prior active-source selections onto the matching
        # freshly-created entries (see the capture in Phase 1a), but NEVER exceed
        # the plan's active-source cap — mirrors the PATCH/import/preselect guards
        # so a re-crawl can't silently push a downgraded tenant over the limit.
        # Restore the most important matches first (importance_rank asc), up to cap.
        if preserved_active_urls and report.tenant_id is not None:
            max_sources = _active_source_cap(db, report.tenant_id)
            candidates = (
                db.query(AuditEntry)
                .filter(
                    AuditEntry.audit_report_id == report.id,
                    AuditEntry.url.in_(preserved_active_urls),
                    AuditEntry.status == AuditEntryStatus.PROPOSED,
                )
                .order_by(AuditEntry.importance_rank.asc())
            )
            if max_sources is not None:  # None == unlimited
                candidates = candidates.limit(max_sources)
            for entry in candidates.all():
                entry.status = AuditEntryStatus.ACTIVE
            db.flush()

        total_entry_count = len(existing_entries) + len(classified)

        # ------------------------------------------------------------------ #
        # Phase 5: expected-content map                                        #
        # ------------------------------------------------------------------ #
        expected_map = await gaps.build_expected_map(profile_dict, unanswered_questions=[])

        # A failed expected-map (empty despite real content) must NOT publish a
        # misleading 0-score/0-gap report. build_expected_map already retries; if
        # it is still empty while we have content, the analyze step failed — fail
        # the audit with a reason so the UI shows "retry", not a fake 0.
        if not expected_map and total_entry_count > 0:
            # ScanUnreadable + ScanQuality already imported in Phase 2b above.
            report.scan_quality = ScanQuality.RENDER_FAILED
            report.scan_quality_reason = "We couldn't finish analyzing your site. Please try again."
            db.commit()
            raise ScanUnreadable(ScanQuality.RENDER_FAILED, "empty expected_map with content")

        # ------------------------------------------------------------------ #
        # Phase 6: gap analysis                                               #
        # ------------------------------------------------------------------ #
        report.gaps = gaps.compute_gaps(expected_map, found_kinds, found_categories)

        # ------------------------------------------------------------------ #
        # Phase 7: scoring — aligned with gaps found-set definition           #
        # Gaps treats an item as found when kind ∈ found_kinds OR             #
        # item-label ∈ found_categories (case-insensitive).  We must use the  #
        # same two-condition rule here so score and gaps tell the same story. #
        # ------------------------------------------------------------------ #
        found_categories_lower = {c.lower() for c in found_categories}
        found_items: set[str] = {
            str(item.get("item", "")).lower()
            for item in expected_map
            if (
                str(item.get("kind", "")) in found_kinds
                or str(item.get("item", "")).lower() in found_categories_lower
            )
        }
        s = scoring.score(expected_map, found_items)
        report.knowledge_score = s["knowledge"]
        report.support_score = s["support"]
        report.conversion_score = s["conversion"]
        report.overall_score = s["overall"]
        report.projected_score = s["projected"]
        report.summary_stats = {
            "kinds": sorted(found_kinds),
            "entries": total_entry_count,
        }

        # ------------------------------------------------------------------ #
        # Phase 8: persist COMPLETED                                          #
        # ------------------------------------------------------------------ #
        report.status = AuditStatus.COMPLETED
        report.completed_at = datetime.now(UTC)
        db.commit()
        _update_job(
            db,
            report.processing_job_id,
            status=JobStatus.COMPLETED,
            completed_at=datetime.now(UTC),
        )

    except Exception as exc:  # noqa: BLE001
        logger.error(
            "run_audit failed for audit_report_id={}: {}", audit_report_id, exc, exc_info=True
        )
        # Recover: clear any pending dirty state via expire_all (avoids a bare
        # rollback that would destroy the outer transaction in test environments
        # that use a connection-level transaction for rollback isolation).
        # Then re-load both the report and its job and mark them FAILED together
        # in one commit so the Scanning screen never gets stuck in PROCESSING.
        try:
            db.expire_all()
            _report = db.query(AuditReport).filter(AuditReport.id == audit_report_id).one_or_none()
            if _report is not None:
                _report.status = AuditStatus.FAILED
                _job_id = _report.processing_job_id
                if _job_id:
                    _job = db.query(ProcessingJob).filter(ProcessingJob.id == _job_id).one_or_none()
                    if _job is not None:
                        _job.status = JobStatus.FAILED
                        _job.error_message = str(exc)
                        _job.completed_at = datetime.now(UTC)
                db.commit()
                report = _report
        except Exception as recovery_exc:  # noqa: BLE001
            logger.error("run_audit recovery commit failed: {}", recovery_exc, exc_info=True)
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass

    try:
        db.refresh(report)
    except Exception:  # noqa: BLE001
        pass
    return report


async def run_website_audit(ctx: dict, audit_report_id: str) -> dict:
    """ARQ entrypoint — opens its own DB session, calls run_audit, never crashes the worker."""
    from app.db.session import SessionLocal

    db: Session = SessionLocal()
    try:
        # Read resume flag from the processing job's input_data so claim_audit's
        # decision (stale=fresh crawl, fresh=incremental) is honoured by the worker.
        report_obj = (
            db.query(AuditReport).filter(AuditReport.id == UUID(audit_report_id)).one_or_none()
        )
        resume = False
        if report_obj and report_obj.processing_job_id:
            job = (
                db.query(ProcessingJob)
                .filter(ProcessingJob.id == report_obj.processing_job_id)
                .one_or_none()
            )
            if job and job.input_data:
                resume = bool(job.input_data.get("resume", False))

        report = await run_audit(db, UUID(audit_report_id), resume=resume)
        return {
            "success": report.status == AuditStatus.COMPLETED,
            "audit_report_id": str(report.id),
            "status": report.status.value,
        }
    except Exception as exc:
        logger.error(
            "run_website_audit outer error for {}: {}", audit_report_id, exc, exc_info=True
        )
        return {"success": False, "error": str(exc)}
    finally:
        db.close()
