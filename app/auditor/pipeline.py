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
) -> AuditReport:
    """Orchestrate all auditor phases and persist results.

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
            db, report.processing_job_id,
            status=JobStatus.PROCESSING,
            started_at=datetime.now(UTC),
        )

        # ------------------------------------------------------------------ #
        # Phase 1: URL discovery                                              #
        # ------------------------------------------------------------------ #
        urls = await discovery.discover(
            report.url,
            _max_pages(db, report),
            fetch_text=fetch_text,
        )
        report.pages_discovered = len(urls)
        _update_job(
            db, report.processing_job_id,
            total_items=len(urls),
            processed_items=0,
        )
        db.commit()

        # ------------------------------------------------------------------ #
        # Phase 2: render / crawl + extract                                   #
        # ------------------------------------------------------------------ #
        robots = await crawler.fetch_robots(report.url, fetch_text=fetch_text)
        raw_pages = await crawler.crawl(
            urls,
            page_renderer=page_renderer,
            robots_disallowed=robots,
        )
        extracted = extraction.extract_pages(raw_pages)
        report.pages_scanned = len(extracted)
        _update_job(
            db, report.processing_job_id,
            processed_items=len(extracted),
        )
        db.commit()

        # ------------------------------------------------------------------ #
        # Phase 3: business profile synthesis / reuse                         #
        # ------------------------------------------------------------------ #
        report.status = AuditStatus.ANALYZING
        db.commit()

        if report.tenant_id is not None:
            profile_obj, _sig = await profile.synthesize_or_reuse(
                db, report.tenant_id, extracted, prior_signature=None
            )
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
        classified = await classify.classify_pages(extracted, profile_dict)
        found_kinds: set[str] = set()
        found_categories: set[str] = set()
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

        # ------------------------------------------------------------------ #
        # Phase 5: expected-content map                                        #
        # ------------------------------------------------------------------ #
        expected_map = await gaps.build_expected_map(
            profile_dict, unanswered_questions=[]
        )

        # ------------------------------------------------------------------ #
        # Phase 6: gap analysis                                               #
        # ------------------------------------------------------------------ #
        report.gaps = gaps.compute_gaps(expected_map, found_kinds, found_categories)

        # ------------------------------------------------------------------ #
        # Phase 7: scoring                                                    #
        # ------------------------------------------------------------------ #
        # found_items = expected items already satisfied (kind overlaps what we found)
        found_items: set[str] = {
            str(item.get("item", "")).lower()
            for item in expected_map
            if str(item.get("kind", "")) in found_kinds
        }
        s = scoring.score(expected_map, found_items)
        report.knowledge_score = s["knowledge"]
        report.support_score = s["support"]
        report.conversion_score = s["conversion"]
        report.overall_score = s["overall"]
        report.projected_score = s["projected"]
        report.summary_stats = {
            "kinds": sorted(found_kinds),
            "entries": len(classified),
        }

        # ------------------------------------------------------------------ #
        # Phase 8: persist COMPLETED                                          #
        # ------------------------------------------------------------------ #
        report.status = AuditStatus.COMPLETED
        report.completed_at = datetime.now(UTC)
        db.commit()
        _update_job(
            db, report.processing_job_id,
            status=JobStatus.COMPLETED,
            completed_at=datetime.now(UTC),
        )

    except Exception as exc:  # noqa: BLE001
        logger.error(
            "run_audit failed for audit_report_id={}: {}", audit_report_id, exc, exc_info=True
        )
        try:
            report.status = AuditStatus.FAILED
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
        _update_job(
            db, report.processing_job_id,
            status=JobStatus.FAILED,
            error_message=str(exc),
            completed_at=datetime.now(UTC),
        )

    db.refresh(report)
    return report


async def run_website_audit(ctx: dict, audit_report_id: str) -> dict:
    """ARQ entrypoint — opens its own DB session, calls run_audit, never crashes the worker."""
    from app.db.session import SessionLocal

    db: Session = SessionLocal()
    try:
        report = await run_audit(db, UUID(audit_report_id))
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
