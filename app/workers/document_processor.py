"""Background worker for processing document ingestion jobs."""

import os
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from loguru import logger
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models.job import JobStatus, ProcessingJob
from app.db.models.knowledge import SourceStatus, SourceType
from app.db.session import SessionLocal
from app.services.knowledge_service import KnowledgeService
from app.utils.embedding import embedding_service


def _set_attr(obj: object, name: str, value: object) -> None:
    setattr(obj, name, value)


async def broadcast_job_status(job_id: UUID, update: dict):
    """
    Broadcast job status update via WebSocket.

    Args:
        job_id: Job UUID
        update: Update dictionary with status/progress
    """
    try:
        from app.utils.websocket_manager import websocket_manager

        await websocket_manager.broadcast_job_update(job_id, update)
    except Exception as e:
        logger.warning(f"Failed to broadcast job update: {e}")


# Rate limiter for Google API (10 calls per minute to stay safe)
# NOTE: Rate limiting is now handled by the embedding service itself
# using Redis-based rate limiter (app/utils/rate_limiter.py).
# This ensures rate limits are enforced across all processes and
# persist through application restarts.


async def process_document_ingestion(
    ctx: dict,
    job_id: str,
) -> dict:
    """
    Background task to process document ingestion with rate limiting.

    This is the main worker function called by ARQ.

    Args:
        ctx: ARQ context (contains redis pool, etc.)
        job_id: UUID of the ProcessingJob record

    Returns:
        dict with job results
    """
    db: Session = SessionLocal()
    job_uuid = UUID(job_id)
    job: ProcessingJob | None = None

    try:
        # Get job from database
        job = db.query(ProcessingJob).filter(ProcessingJob.id == job_uuid).first()

        if not job:
            logger.error(f"Job {job_id} not found in database")
            return {"success": False, "error": "Job not found"}

        # Mark job as processing
        _set_attr(job, "status", JobStatus.PROCESSING)
        _set_attr(job, "started_at", datetime.now(UTC))
        db.commit()

        # Broadcast status update via WebSocket
        await broadcast_job_status(
            job_uuid,
            {
                "job_id": job_id,
                "status": "processing",
                "message": "Starting document processing...",
            },
        )

        logger.info(f"Starting document ingestion job {job_id}")

        # Extract input data
        input_data = cast(dict[str, Any], job.input_data or {})
        tenant_id = UUID(str(input_data["tenant_id"]))
        document_text = str(input_data["document_text"])
        document_name = str(input_data["document_name"])
        source_type = SourceType(str(input_data["source_type"]))  # Convert string to enum
        document_metadata = cast(dict[str, Any], input_data.get("metadata", {}))
        uploaded_by = (
            UUID(str(input_data["uploaded_by"])) if input_data.get("uploaded_by") else None
        )
        source_category = str(input_data.get("source_category", "file"))
        source_url = cast(str | None, input_data.get("source_url"))
        file_size_bytes = cast(int | None, input_data.get("file_size_bytes"))

        # Initialize knowledge service
        knowledge_service = KnowledgeService(db)

        # Step 1: Create source and chunk text (fast, no API calls)
        logger.info(f"Creating source and chunking document: {document_name}")

        source = await knowledge_service.create_source_and_chunks(
            tenant_id=tenant_id,
            document_text=document_text,
            document_name=document_name,
            source_type=source_type,
            document_metadata=document_metadata,
            uploaded_by=uploaded_by,
            source_category=source_category,
            source_url=source_url,
            file_size_bytes=file_size_bytes,
        )

        # Update job with total chunks
        # Query chunks directly as DB objects (not Pydantic models) so we can update them
        from app.db.models.knowledge import KnowledgeChunk

        chunks = (
            db.query(KnowledgeChunk)
            .filter(KnowledgeChunk.source_id == source.id, KnowledgeChunk.is_active.is_(True))
            .order_by(KnowledgeChunk.chunk_index)
            .all()
        )

        _set_attr(job, "total_items", len(chunks))
        db.commit()

        # Broadcast chunking complete
        await broadcast_job_status(
            job_uuid,
            {
                "job_id": job_id,
                "status": "processing",
                "total_items": len(chunks),
                "processed_items": 0,
                "progress_percent": 0,
                "message": f"Document chunked into {len(chunks)} parts. Starting embedding generation...",
            },
        )

        logger.info(
            f"Created {len(chunks)} chunks. Starting embedding generation with rate limiting..."
        )

        # Ensure Qdrant collection exists before processing
        from app.utils.qdrant_client import qdrant_manager

        if not qdrant_manager.ensure_collection():
            raise RuntimeError("Failed to ensure Qdrant collection exists")

        # Step 2: Generate embeddings with rate limiting
        successful_chunks = []
        failed_chunks = []

        for i, chunk in enumerate(chunks):
            try:
                # Generate embedding (rate limiting handled by embedding service)
                embedding = await embedding_service.generate_embedding(chunk.content)

                if embedding:
                    # Store in Qdrant
                    from app.utils.qdrant_client import qdrant_manager

                    # Use chunk UUID as vector_id (Qdrant requires UUID or integer)
                    vector_id = str(chunk.id)
                    qdrant_manager.upsert_vector(
                        vector_id=vector_id,
                        embedding=embedding,
                        tenant_id=tenant_id,
                        chunk_id=chunk.id,
                        source_id=source.id,
                        content=chunk.content,
                        metadata={
                            "chunk_index": chunk.chunk_index,
                        },
                    )

                    # Update chunk with vector_id
                    _set_attr(chunk, "vector_id", vector_id)
                    successful_chunks.append(chunk.id)

                    logger.debug(f"✓ Embedded chunk {i+1}/{len(chunks)}")
                else:
                    failed_chunks.append(chunk.id)
                    logger.warning(f"✗ Failed to embed chunk {i+1}/{len(chunks)}")

                # Update job progress
                _set_attr(job, "processed_items", i + 1)
                db.commit()

                # Broadcast progress update (every chunk)
                progress_percent = int((i + 1) / len(chunks) * 100)
                await broadcast_job_status(
                    job_uuid,
                    {
                        "job_id": job_id,
                        "status": "processing",
                        "processed_items": i + 1,
                        "total_items": len(chunks),
                        "progress_percent": progress_percent,
                        "message": f"Processing chunk {i+1}/{len(chunks)}...",
                    },
                )

            except Exception as e:
                failed_chunks.append(chunk.id)
                logger.error(f"Error processing chunk {i+1}: {e}")

                # Continue with next chunk (don't fail entire job)
                _set_attr(job, "processed_items", i + 1)
                _set_attr(job, "failed_items", len(failed_chunks))
                db.commit()

        # Step 3: Finalize source status
        # Re-query source as DB object (the 'source' variable is a Pydantic model)
        from app.db.models.knowledge import KnowledgeSource

        db_source = db.query(KnowledgeSource).filter(KnowledgeSource.id == source.id).first()
        if not db_source:
            raise RuntimeError(f"Source {source.id} not found for job {job_id}")

        if len(successful_chunks) > 0:
            _set_attr(db_source, "status", SourceStatus.ACTIVE)
            # Recompute chunk_count from actual chunks to prevent drift
            knowledge_service.recompute_chunk_count(source.id)
            _set_attr(job, "status", JobStatus.COMPLETED)
            _set_attr(
                job,
                "result_data",
                {
                    "source_id": str(source.id),
                    "source_name": source.name,
                    "chunks_created": len(successful_chunks),
                    "chunks_failed": len(failed_chunks),
                },
            )
            logger.success(
                f"✓ Job {job_id} completed: {len(successful_chunks)}/{len(chunks)} chunks processed"
            )
        else:
            _set_attr(db_source, "status", SourceStatus.FAILED)
            _set_attr(job, "status", JobStatus.FAILED)
            _set_attr(job, "error_message", "All chunks failed to embed")
            logger.error(f"✗ Job {job_id} failed: No chunks successfully embedded")

        _set_attr(job, "completed_at", datetime.now(UTC))
        _set_attr(job, "failed_items", len(failed_chunks))
        db.commit()

        # Broadcast final status via WebSocket
        await broadcast_job_status(
            job_uuid,
            {
                "job_id": job_id,
                "status": job.status.value,
                "progress_percent": (
                    100 if job.status == JobStatus.COMPLETED else job.progress_percent
                ),
                "processed_items": len(successful_chunks),
                "total_items": len(chunks),
                "result_data": job.result_data,
                "error_message": job.error_message,
                "message": (
                    "Processing complete!"
                    if job.status == JobStatus.COMPLETED
                    else "Processing failed"
                ),
            },
        )

        # Send email notification (if configured)
        await send_job_completion_email(job, db)

        return {
            "success": job.status == JobStatus.COMPLETED,
            "source_id": str(source.id) if source else None,
            "chunks_processed": len(successful_chunks),
            "chunks_failed": len(failed_chunks),
        }

    except Exception as e:
        logger.error(f"Critical error in job {job_id}: {e}", exc_info=True)

        # Mark job as failed
        if job:
            _set_attr(job, "status", JobStatus.FAILED)
            _set_attr(job, "error_message", str(e))
            _set_attr(job, "completed_at", datetime.now(UTC))
            db.commit()

            # Broadcast error via WebSocket
            await broadcast_job_status(
                job_uuid,
                {
                    "job_id": job_id,
                    "status": "failed",
                    "error_message": str(e),
                    "message": "Job failed due to an error",
                },
            )

        return {"success": False, "error": str(e)}

    finally:
        db.close()


async def send_job_completion_email(job: ProcessingJob, db: Session):
    """
    Send email notification when job completes.

    Args:
        job: Completed job
        db: Database session
    """
    try:
        # Only send if email is configured
        if not settings.SMTP_HOST or not settings.EMAILS_FROM_EMAIL:
            logger.debug("Email not configured, skipping notification")
            return

        # Get user email
        from app.db.models.user import User

        if not job.created_by:
            logger.debug("No user associated with job, skipping email")
            return

        user = db.query(User).filter(User.id == job.created_by).first()
        if not user:
            logger.warning(f"User {job.created_by} not found for job {job.id}")
            return

        # Get tenant name
        from app.db.models.tenant import Tenant

        tenant = db.query(Tenant).filter(Tenant.id == job.tenant_id).first()
        tenant_name = tenant.display_name if tenant else "Unknown"

        # Compose email
        subject = (
            "✓ Document Processing Complete"
            if job.status == JobStatus.COMPLETED
            else "✗ Document Processing Failed"
        )

        if job.status == JobStatus.COMPLETED:
            result = cast(dict[str, Any], job.result_data or {})
            body = f"""
Hello {user.full_name},

Your document has been successfully processed and is now available in the knowledge base.

Details:
- Document: {result.get('source_name', 'Unknown')}
- Tenant: {tenant_name}
- Chunks Created: {result.get('chunks_created', 0)}
- Status: ✓ Complete

You can now start using this knowledge in your AI chatbot.

Best regards,
Luran Team
            """
        else:
            body = f"""
Hello {user.full_name},

Unfortunately, your document processing job failed.

Error: {job.error_message}

Please try again or contact support if the issue persists.

Best regards,
Luran Team
            """

        # Send email (using your existing email service)
        from app.utils.email import send_email

        await send_email(
            email_to=user.email,
            subject=subject,
            body=body,
        )

        logger.info(f"Sent job completion email to {user.email}")

    except Exception as e:
        logger.error(f"Failed to send job completion email: {e}")
        # Don't fail the job if email fails


# ============================================================================
# Automated Email Notification Workers
# ============================================================================


async def check_trial_expiration_3days(ctx: dict) -> dict:
    """
    Check for trial/free-tier expirations and send reminder emails.

    Scheduled: Daily at 9:00 AM UTC

    Args:
        ctx: ARQ context

    Returns:
        Result dict with count of emails sent
    """
    from datetime import timedelta

    from sqlalchemy import and_, select

    from app.db.models.tenant import Tenant
    from app.services.email_notification_service import EmailNotificationService

    # Config-driven reminder offsets (days before trial expiry).
    # Process longest window first for clearer logs and expected reminder cadence.
    reminder_windows_days = sorted(settings.get_trial_expiry_reminder_days(), reverse=True)

    logger.info(
        "Running trial expiration reminders for windows: {}",
        ",".join(map(str, reminder_windows_days)),
    )

    db = SessionLocal()
    emails_sent = 0
    tenants_checked = 0

    try:
        email_service = EmailNotificationService(db)

        for days_remaining in reminder_windows_days:
            target_date = datetime.now(UTC) + timedelta(days=days_remaining)
            date_start = target_date.replace(hour=0, minute=0, second=0, microsecond=0)
            date_end = target_date.replace(hour=23, minute=59, second=59, microsecond=999999)

            query = select(Tenant).where(
                and_(
                    Tenant.subscription_status.in_(["trialing", "trial"]),
                    Tenant.trial_ends_at >= date_start,
                    Tenant.trial_ends_at <= date_end,
                )
            )

            result = db.execute(query)
            expiring_tenants = result.scalars().all()
            tenants_checked += len(expiring_tenants)

            logger.info(
                "Found {} tenants with trials expiring in {} day(s)",
                len(expiring_tenants),
                days_remaining,
            )

            for tenant in expiring_tenants:
                try:
                    success = await email_service.send_trial_expiration_reminder(
                        tenant_id=tenant.id, days_remaining=days_remaining
                    )
                    if success:
                        emails_sent += 1
                        logger.info(
                            "Sent trial expiration reminder to tenant {} ({} day(s) remaining)",
                            tenant.name,
                            days_remaining,
                        )
                    else:
                        logger.warning(
                            "Failed to send reminder to tenant {} ({} day(s) remaining)",
                            tenant.name,
                            days_remaining,
                        )
                except Exception as e:
                    logger.error(
                        "Error sending reminder to tenant {} ({} day(s) remaining): {}",
                        tenant.name,
                        days_remaining,
                        e,
                    )

        logger.info(
            "Trial expiration reminder run complete. Sent {}/{} emails",
            emails_sent,
            tenants_checked,
        )

        return {
            "status": "success",
            "emails_sent": emails_sent,
            "tenants_checked": tenants_checked,
            "windows": reminder_windows_days,
        }

    except Exception as e:
        logger.error(f"Error in trial expiration check: {e}")
        return {
            "status": "error",
            "error": str(e),
            "emails_sent": emails_sent,
        }
    finally:
        db.close()


async def expire_lapsed_trials(ctx: dict) -> dict:
    """Transition tenants from `trialing` to `trial_expired` once their trial ends.

    Until this runs, the only path that flips the status is the lazy check in
    `get_current_user` (deps.py) and the chat endpoint — meaning tenants who
    never log back in after trial expiry stay `trialing` forever, which breaks
    cohort filters, analytics, and the promotional-grants win-back wizard.

    Scheduled: Daily at 9:05 AM UTC (just after the reminder run).
    """
    from sqlalchemy import update

    from app.db.models.tenant import Tenant

    db = SessionLocal()
    try:
        now = datetime.now(UTC)
        stmt = (
            update(Tenant)
            .where(
                Tenant.subscription_status.in_(["trialing", "trial"]),
                Tenant.trial_ends_at.is_not(None),
                Tenant.trial_ends_at < now,
            )
            .values(subscription_status="trial_expired", updated_at=now)
        )
        result = db.execute(stmt)
        db.commit()
        transitioned = result.rowcount  # type: ignore[attr-defined]
        logger.info(
            "expire_lapsed_trials: transitioned {} tenants to trial_expired",
            transitioned,
        )
        return {"status": "success", "transitioned": transitioned}
    except Exception as e:
        db.rollback()
        logger.error(f"Error in expire_lapsed_trials: {e}")
        return {"status": "error", "error": str(e)}
    finally:
        db.close()


async def send_subscription_confirmations(ctx: dict) -> dict:
    """
    Send confirmation emails for successful payments in the last 10 minutes.

    Scheduled: Every 5 minutes

    Args:
        ctx: ARQ context

    Returns:
        Result dict with count of emails sent
    """
    from datetime import timedelta

    from sqlalchemy import and_, select

    from app.db.models.payment import PaymentTransaction
    from app.services.email_notification_service import EmailNotificationService

    logger.info("Running subscription confirmation email job")

    db = SessionLocal()
    emails_sent = 0

    try:
        # Find successful payments in the last 10 minutes
        cutoff_time = datetime.now(UTC) - timedelta(minutes=10)

        query = select(PaymentTransaction).where(
            and_(
                PaymentTransaction.status == "success",
                PaymentTransaction.created_at >= cutoff_time,
            )
        )

        result = db.execute(query)
        recent_payments = result.scalars().all()

        logger.info(f"Found {len(recent_payments)} successful payments in last 10 minutes")

        # Send confirmation emails
        email_service = EmailNotificationService(db)

        for payment in recent_payments:
            try:
                success = await email_service.send_subscription_confirmation(
                    tenant_id=payment.tenant_id, payment_id=payment.id
                )
                if success:
                    emails_sent += 1
                    logger.info(f"Sent subscription confirmation for payment {payment.id}")
            except Exception as e:
                logger.error(f"Error sending confirmation for payment {payment.id}: {e}")

        logger.info(
            f"Subscription confirmation job complete. Sent {emails_sent}/{len(recent_payments)} emails"
        )

        return {
            "status": "success",
            "emails_sent": emails_sent,
            "payments_checked": len(recent_payments),
        }

    except Exception as e:
        logger.error(f"Error in subscription confirmation job: {e}")
        return {
            "status": "error",
            "error": str(e),
            "emails_sent": emails_sent,
        }
    finally:
        db.close()


async def check_subscription_renewal_reminders(ctx: dict) -> dict:
    """
    Send paid subscription auto-renewal reminders (7/3/1 days by default).

    Scheduled: Daily at 10:00 AM UTC
    """
    from datetime import timedelta

    from sqlalchemy import and_, select

    from app.db.models.tenant import Tenant
    from app.services.email_notification_service import EmailNotificationService

    reminder_windows_days = sorted(settings.get_subscription_renewal_reminder_days(), reverse=True)

    logger.info(
        "Running subscription renewal reminders for windows: {}",
        ",".join(map(str, reminder_windows_days)),
    )

    db = SessionLocal()
    emails_sent = 0
    tenants_checked = 0

    try:
        email_service = EmailNotificationService(db)

        base_query = select(Tenant).where(
            and_(
                Tenant.subscription_status == "active",
                Tenant.subscription_tier != "trial",
                Tenant.billing_cycle.isnot(None),
            )
        )
        tenant_candidates = db.execute(base_query).scalars().all()
        logger.info(
            "Found {} active paid tenants to evaluate for renewal reminders", len(tenant_candidates)
        )

        for days_remaining in reminder_windows_days:
            target_date = datetime.now(UTC) + timedelta(days=days_remaining)
            date_start = target_date.replace(hour=0, minute=0, second=0, microsecond=0)
            date_end = target_date.replace(hour=23, minute=59, second=59, microsecond=999999)

            for tenant in tenant_candidates:
                next_renewal_at = await email_service.estimate_next_renewal_at(tenant.id)
                if not next_renewal_at:
                    continue
                if not (date_start <= next_renewal_at <= date_end):
                    continue

                tenants_checked += 1
                try:
                    success = await email_service.send_subscription_renewal_reminder(
                        tenant_id=tenant.id,
                        days_remaining=days_remaining,
                        next_renewal_at=next_renewal_at,
                    )
                    if success:
                        emails_sent += 1
                        logger.info(
                            "Sent renewal reminder to tenant {} ({} day(s) remaining)",
                            tenant.name,
                            days_remaining,
                        )
                    else:
                        logger.warning(
                            "Failed renewal reminder for tenant {} ({} day(s) remaining)",
                            tenant.name,
                            days_remaining,
                        )
                except Exception as e:
                    logger.error(
                        "Error sending renewal reminder to tenant {} ({} day(s) remaining): {}",
                        tenant.name,
                        days_remaining,
                        e,
                    )

        logger.info(
            "Subscription renewal reminder run complete. Sent {}/{} emails",
            emails_sent,
            tenants_checked,
        )
        return {
            "status": "success",
            "emails_sent": emails_sent,
            "tenants_checked": tenants_checked,
            "windows": reminder_windows_days,
        }
    except Exception as e:
        logger.error(f"Error in subscription renewal reminders job: {e}")
        return {
            "status": "error",
            "error": str(e),
            "emails_sent": emails_sent,
        }
    finally:
        db.close()


async def send_payment_failure_emails(ctx: dict) -> dict:
    """
    Send notification emails for failed payments in the last 2 hours.

    Scheduled: Every 30 minutes

    Args:
        ctx: ARQ context

    Returns:
        Result dict with count of emails sent
    """
    from datetime import timedelta

    from sqlalchemy import and_, select

    from app.db.models.payment import PaymentTransaction
    from app.services.email_notification_service import EmailNotificationService

    logger.info("Running payment failure notification job")

    db = SessionLocal()
    emails_sent = 0

    try:
        # Find failed payments in the last 2 hours
        cutoff_time = datetime.now(UTC) - timedelta(hours=2)

        query = select(PaymentTransaction).where(
            and_(
                PaymentTransaction.status == "failed",
                PaymentTransaction.created_at >= cutoff_time,
            )
        )

        result = db.execute(query)
        failed_payments = result.scalars().all()

        logger.info(f"Found {len(failed_payments)} failed payments in last 2 hours")

        # Send failure notification emails
        email_service = EmailNotificationService(db)

        for payment in failed_payments:
            try:
                success = await email_service.send_payment_failure_notification(
                    tenant_id=payment.tenant_id,
                    payment_id=payment.id,
                    grace_days=7,
                    failure_reason=None,  # Could extract from payment metadata
                )
                if success:
                    emails_sent += 1
                    logger.info(f"Sent payment failure notification for payment {payment.id}")
            except Exception as e:
                logger.error(f"Error sending failure notification for payment {payment.id}: {e}")

        logger.info(
            f"Payment failure job complete. Sent {emails_sent}/{len(failed_payments)} emails"
        )

        return {
            "status": "success",
            "emails_sent": emails_sent,
            "payments_checked": len(failed_payments),
        }

    except Exception as e:
        logger.error(f"Error in payment failure notification job: {e}")
        return {
            "status": "error",
            "error": str(e),
            "emails_sent": emails_sent,
        }
    finally:
        db.close()


async def send_scheduled_platform_email_campaigns(ctx: dict) -> dict:
    """
    Send platform email campaigns that are due for delivery.

    Scheduled: Every 5 minutes
    """
    from app.services.platform_email_campaign_service import PlatformEmailCampaignService

    logger.info("Running scheduled platform email campaign job")

    db = SessionLocal()
    campaigns_processed = 0

    try:
        service = PlatformEmailCampaignService(db)
        campaigns_processed = service.send_due_campaigns()

        logger.info(
            f"Scheduled platform email campaign job complete. Processed {campaigns_processed} campaign(s)"
        )

        return {
            "status": "success",
            "campaigns_processed": campaigns_processed,
        }
    except Exception as e:
        logger.error(f"Error in scheduled platform email campaign job: {e}")
        return {
            "status": "error",
            "error": str(e),
            "campaigns_processed": campaigns_processed,
        }
    finally:
        db.close()


async def aggregate_daily_metrics(ctx: dict) -> dict:
    """
    Aggregate platform-wide metrics for analytics dashboard.

    Scheduled: Daily at 1:00 AM UTC

    Args:
        ctx: ARQ context

    Returns:
        Result dict with aggregation status
    """
    from datetime import timedelta

    from sqlalchemy import func, select

    from app.db.models.audit import AnalyticsMetric
    from app.db.models.tenant import Tenant

    logger.info("Running daily metrics aggregation")

    db = SessionLocal()
    metrics_created = 0

    try:
        # Calculate yesterday's date range
        today = datetime.now(UTC).date()
        yesterday = today - timedelta(days=1)
        period_start = datetime.combine(yesterday, datetime.min.time()).replace(tzinfo=UTC)
        period_end = datetime.combine(yesterday, datetime.max.time()).replace(tzinfo=UTC)

        # Aggregate tenant count by subscription tier
        tier_query = (
            select(Tenant.subscription_tier, func.count(Tenant.id).label("count"))
            .where(Tenant.subscription_status == "active")
            .group_by(Tenant.subscription_tier)
        )

        tier_result = db.execute(tier_query)
        tier_counts = tier_result.all()

        # Store tier metrics
        for tier, count in tier_counts:
            metric = AnalyticsMetric(
                tenant_id=None,  # Platform-wide metric
                metric_type=f"active_tenants_{tier}",
                metric_value=float(count),
                period_start=period_start,
                period_end=period_end,
                meta={"tier": tier},
            )
            db.add(metric)
            metrics_created += 1

        # Total active tenants
        total_active_query = select(func.count(Tenant.id)).where(
            Tenant.subscription_status == "active"
        )
        total_active_result = db.execute(total_active_query)
        total_active = total_active_result.scalar()

        metric = AnalyticsMetric(
            tenant_id=None,
            metric_type="total_active_tenants",
            metric_value=float(total_active or 0),
            period_start=period_start,
            period_end=period_end,
            meta={},
        )
        db.add(metric)
        metrics_created += 1

        # Total trialing tenants
        total_trial_query = select(func.count(Tenant.id)).where(
            Tenant.subscription_status == "trialing"
        )
        total_trial_result = db.execute(total_trial_query)
        total_trial = total_trial_result.scalar()

        metric = AnalyticsMetric(
            tenant_id=None,
            metric_type="total_trialing_tenants",
            metric_value=float(total_trial or 0),
            period_start=period_start,
            period_end=period_end,
            meta={},
        )
        db.add(metric)
        metrics_created += 1

        db.commit()

        logger.info(
            f"Daily metrics aggregation complete. Created {metrics_created} metrics for {yesterday}"
        )

        return {
            "status": "success",
            "metrics_created": metrics_created,
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(),
        }

    except Exception as e:
        logger.error(f"Error in daily metrics aggregation: {e}")
        db.rollback()
        return {
            "status": "error",
            "error": str(e),
            "metrics_created": metrics_created,
        }
    finally:
        db.close()


async def send_channel_post_notifications(ctx: dict, post_id: str) -> None:
    """Send email notifications to all followers when a channel post is published."""
    from uuid import UUID

    from sqlalchemy import select

    from app.db.models.customer_engagement import TenantChannelPost
    from app.db.models.tenant import Tenant
    from app.services.customer_engagement_service import CustomerEngagementService
    from app.services.email_service import email_service

    logger.info(f"Sending channel post notifications for post {post_id}")

    db = ctx.get("db")
    if not db:
        from app.db.session import SessionLocal

        db = SessionLocal()

    try:
        post = db.execute(
            select(TenantChannelPost).where(TenantChannelPost.id == UUID(post_id))
        ).scalar_one_or_none()

        if not post:
            logger.warning(f"Channel post {post_id} not found")
            return

        tenant = db.execute(select(Tenant).where(Tenant.id == post.tenant_id)).scalar_one_or_none()

        if not tenant:
            logger.warning(f"Tenant not found for post {post_id}")
            return

        service = CustomerEngagementService(db)
        followers = service.get_followers_with_notifications(post.tenant_id)

        if not followers:
            logger.info(f"No followers with notifications for tenant {tenant.id}")
            return

        customer_frontend_url = settings.CUSTOMER_FRONTEND_URL or settings.FRONTEND_URL
        post_url = f"{customer_frontend_url}/app/channels/{post.id}"

        sent_count = 0
        for customer_user, _follow in followers:
            success = email_service.send_channel_update_notification(
                recipient_email=customer_user.email,
                recipient_name=customer_user.full_name,
                business_name=tenant.display_name,
                post_title=post.title,
                post_excerpt=post.excerpt or "",
                post_url=post_url,
            )
            if success:
                sent_count += 1

        logger.info(
            f"Channel post notifications: sent {sent_count}/{len(followers)} "
            f"emails for post {post_id} ({tenant.display_name})"
        )

    except Exception as e:
        logger.error(f"Failed to send channel post notifications: {e}")
    finally:
        if "db" not in ctx:
            db.close()


async def process_promotional_grant_lifecycle(ctx: dict) -> dict:
    """Daily worker: send reminders and process grant expiries.

    Scheduled: Daily at 09:05 UTC (5 minutes after trial expiration reminders
    to avoid lock contention).
    """
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import and_, select

    from app.db.models.promotional_grant import PromotionalGrant
    from app.services.email_notification_service import EmailNotificationService
    from app.services.promotional_grant_service import PromotionalGrantService

    db = SessionLocal()
    reminders_sent = 0
    expired = 0
    errors = 0
    try:
        email_service = EmailNotificationService(db)
        promo_service = PromotionalGrantService(db)

        # ---- Pass 1: reminders at T-14, T-7, T-1 ----
        for days_remaining in [14, 7, 1]:
            target_date = datetime.now(UTC) + timedelta(days=days_remaining)
            date_start = target_date.replace(hour=0, minute=0, second=0, microsecond=0)
            date_end = target_date.replace(hour=23, minute=59, second=59, microsecond=999999)
            query = select(PromotionalGrant).where(
                and_(
                    PromotionalGrant.status == "active",
                    PromotionalGrant.grant_ends_at >= date_start,
                    PromotionalGrant.grant_ends_at <= date_end,
                )
            )
            grants = db.execute(query).scalars().all()
            for grant in grants:
                try:
                    success = await email_service.send_promo_expiration_reminder(
                        grant_id=grant.id, days_remaining=days_remaining
                    )
                    if success:
                        reminders_sent += 1
                except Exception as e:
                    logger.error(f"Promo reminder error for grant {grant.id}: {e}")
                    errors += 1

        # ---- Pass 2: expire past-due grants ----
        try:
            expired_ids = promo_service.expire_grants_due()
            expired = len(expired_ids)
        except Exception as e:
            logger.error(f"Promo expiry pass error: {e}")
            errors += 1
            expired_ids = []

        for grant_id in expired_ids:
            try:
                await email_service.send_promo_expired_email(grant_id=grant_id)
            except Exception as e:
                logger.error(f"Promo expired email error for grant {grant_id}: {e}")
                errors += 1

        logger.info(
            "Promo lifecycle complete. reminders={} expired={} errors={}",
            reminders_sent,
            expired,
            errors,
        )
    finally:
        db.close()

    return {"reminders_sent": reminders_sent, "expired": expired, "errors": errors}


async def send_promo_granted_emails_batch(ctx: dict, grant_ids: list[str]) -> dict:
    """Send `you've been granted Pro` emails for a batch of grants.

    Enqueued by the bulk-grant endpoint right after grants are created. The
    underlying send is idempotent (7-day window), so retries / duplicate
    enqueues are safe.
    """
    import uuid

    from app.services.email_notification_service import EmailNotificationService

    db = SessionLocal()
    sent = 0
    failed = 0
    try:
        email_service = EmailNotificationService(db)
        for gid in grant_ids:
            try:
                ok = await email_service.send_promo_granted_email(grant_id=uuid.UUID(gid))
                if ok:
                    sent += 1
                else:
                    failed += 1
            except Exception as e:
                logger.error(f"send_promo_granted_emails_batch: failed for {gid}: {e}")
                failed += 1
        logger.info(
            "send_promo_granted_emails_batch: sent {}, failed {}, total {}",
            sent,
            failed,
            len(grant_ids),
        )
        return {"status": "success", "sent": sent, "failed": failed, "total": len(grant_ids)}
    except Exception as e:
        logger.error(f"send_promo_granted_emails_batch error: {e}")
        return {"status": "error", "error": str(e), "sent": sent, "failed": failed}
    finally:
        db.close()


# ARQ worker settings
def get_redis_settings():
    """Get Redis settings from environment at runtime."""
    from arq.connections import RedisSettings as ARQRedisSettings

    # Get from environment (respects Docker overrides)
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    logger.info(f"Worker connecting to Redis: {redis_url}")
    return ARQRedisSettings.from_dsn(redis_url)


class WorkerSettings:
    """ARQ worker configuration."""

    # Import scheduled tasks
    from app.workers.conversation_auto_resolver import (  # type: ignore[misc]
        auto_abandon_inactive_conversations,
        auto_resolve_stale_tickets,
    )

    functions = [
        process_document_ingestion,
        auto_abandon_inactive_conversations,
        auto_resolve_stale_tickets,
        check_trial_expiration_3days,
        expire_lapsed_trials,
        check_subscription_renewal_reminders,
        send_subscription_confirmations,
        send_payment_failure_emails,
        send_scheduled_platform_email_campaigns,
        aggregate_daily_metrics,
        send_channel_post_notifications,
        process_promotional_grant_lifecycle,
        send_promo_granted_emails_batch,
    ]
    redis_settings = get_redis_settings()

    # Worker behavior
    max_jobs = 5  # Process max 5 jobs concurrently
    job_timeout = 3600  # 1 hour timeout per job
    keep_result = 86400  # Keep results for 24 hours

    # Retry settings
    max_tries = 3  # Retry failed jobs up to 3 times
    retry_jobs = True

    # Scheduled tasks (cron jobs)
    from arq.cron import cron  # type: ignore[misc]

    cron_jobs = [
        # Auto-abandon inactive conversations - every 5 minutes
        cron(
            auto_abandon_inactive_conversations,
            minute={0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55},
            run_at_startup=True,  # Run once on worker startup
        ),
        # Auto-resolve stale tickets - daily at 2 AM UTC
        cron(
            auto_resolve_stale_tickets,
            hour=2,
            minute=0,
            run_at_startup=False,  # Don't run on startup
        ),
        # Check trial expiration reminders (14/7/3/1/0 days) - daily at 9 AM UTC
        cron(
            check_trial_expiration_3days,
            hour=9,
            minute=0,
            run_at_startup=False,
        ),
        # Transition expired trials from `trialing` to `trial_expired` - daily at 9:05 AM UTC
        cron(
            expire_lapsed_trials,
            hour=9,
            minute=5,
            run_at_startup=True,  # backfill any stale rows on worker start
        ),
        # Check paid subscription auto-renewal reminders (7/3/1 days) - daily at 10 AM UTC
        cron(
            check_subscription_renewal_reminders,
            hour=10,
            minute=0,
            run_at_startup=False,
        ),
        # Send subscription confirmation emails - every 5 minutes
        cron(
            send_subscription_confirmations,
            minute={0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55},
            run_at_startup=False,
        ),
        # Send payment failure notifications - every 30 minutes
        cron(
            send_payment_failure_emails,
            minute={0, 30},
            run_at_startup=False,
        ),
        # Send scheduled platform email campaigns - every 5 minutes
        cron(
            send_scheduled_platform_email_campaigns,
            minute={0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55},
            run_at_startup=False,
        ),
        # Aggregate daily metrics - daily at 1 AM UTC
        cron(
            aggregate_daily_metrics,
            hour=1,
            minute=0,
            run_at_startup=False,
        ),
        # Promotional grant lifecycle (reminders + expiry) - daily at 09:05 UTC
        cron(
            process_promotional_grant_lifecycle,
            hour={9},
            minute={5},
            run_at_startup=False,
        ),
    ]
