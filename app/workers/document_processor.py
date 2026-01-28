"""Background worker for processing document ingestion jobs."""

import asyncio
import os
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from loguru import logger
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import SessionLocal
from app.db.models.job import JobStatus, JobType, ProcessingJob
from app.db.models.knowledge import SourceType, SourceStatus
from app.services.knowledge_service import KnowledgeService
from app.utils.embedding import embedding_service


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

    try:
        # Get job from database
        job = db.query(ProcessingJob).filter(ProcessingJob.id == job_uuid).first()

        if not job:
            logger.error(f"Job {job_id} not found in database")
            return {"success": False, "error": "Job not found"}

        # Mark job as processing
        job.status = JobStatus.PROCESSING
        job.started_at = datetime.now(timezone.utc)
        db.commit()

        # Broadcast status update via WebSocket
        await broadcast_job_status(job_uuid, {
            "job_id": job_id,
            "status": "processing",
            "message": "Starting document processing...",
        })

        logger.info(f"Starting document ingestion job {job_id}")

        # Extract input data
        input_data = job.input_data
        tenant_id = UUID(input_data["tenant_id"])
        document_text = input_data["document_text"]
        document_name = input_data["document_name"]
        source_type = SourceType(input_data["source_type"])  # Convert string to enum
        document_metadata = input_data.get("metadata", {})
        uploaded_by = UUID(input_data["uploaded_by"]) if input_data.get("uploaded_by") else None
        source_category = input_data.get("source_category", "file")
        source_url = input_data.get("source_url")
        file_size_bytes = input_data.get("file_size_bytes")

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
        chunks = db.query(KnowledgeChunk).filter(
            KnowledgeChunk.source_id == source.id,
            KnowledgeChunk.is_active == True
        ).order_by(KnowledgeChunk.chunk_index).all()

        job.total_items = len(chunks)
        db.commit()

        # Broadcast chunking complete
        await broadcast_job_status(job_uuid, {
            "job_id": job_id,
            "status": "processing",
            "total_items": len(chunks),
            "processed_items": 0,
            "progress_percent": 0,
            "message": f"Document chunked into {len(chunks)} parts. Starting embedding generation...",
        })

        logger.info(f"Created {len(chunks)} chunks. Starting embedding generation with rate limiting...")

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
                    chunk.vector_id = vector_id
                    successful_chunks.append(chunk.id)

                    logger.debug(f"✓ Embedded chunk {i+1}/{len(chunks)}")
                else:
                    failed_chunks.append(chunk.id)
                    logger.warning(f"✗ Failed to embed chunk {i+1}/{len(chunks)}")

                # Update job progress
                job.processed_items = i + 1
                db.commit()

                # Broadcast progress update (every chunk)
                progress_percent = int((i + 1) / len(chunks) * 100)
                await broadcast_job_status(job_uuid, {
                    "job_id": job_id,
                    "status": "processing",
                    "processed_items": i + 1,
                    "total_items": len(chunks),
                    "progress_percent": progress_percent,
                    "message": f"Processing chunk {i+1}/{len(chunks)}...",
                })

            except Exception as e:
                failed_chunks.append(chunk.id)
                logger.error(f"Error processing chunk {i+1}: {e}")

                # Continue with next chunk (don't fail entire job)
                job.processed_items = i + 1
                job.failed_items = len(failed_chunks)
                db.commit()

        # Step 3: Finalize source status
        # Re-query source as DB object (the 'source' variable is a Pydantic model)
        from app.db.models.knowledge import KnowledgeSource
        db_source = db.query(KnowledgeSource).filter(KnowledgeSource.id == source.id).first()

        if len(successful_chunks) > 0:
            db_source.status = SourceStatus.ACTIVE
            # Recompute chunk_count from actual chunks to prevent drift
            knowledge_service.recompute_chunk_count(source.id)
            job.status = JobStatus.COMPLETED
            job.result_data = {
                "source_id": str(source.id),
                "source_name": source.name,
                "chunks_created": len(successful_chunks),
                "chunks_failed": len(failed_chunks),
            }
            logger.success(f"✓ Job {job_id} completed: {len(successful_chunks)}/{len(chunks)} chunks processed")
        else:
            db_source.status = SourceStatus.FAILED
            job.status = JobStatus.FAILED
            job.error_message = "All chunks failed to embed"
            logger.error(f"✗ Job {job_id} failed: No chunks successfully embedded")

        job.completed_at = datetime.now(timezone.utc)
        job.failed_items = len(failed_chunks)
        db.commit()

        # Broadcast final status via WebSocket
        await broadcast_job_status(job_uuid, {
            "job_id": job_id,
            "status": job.status.value,
            "progress_percent": 100 if job.status == JobStatus.COMPLETED else job.progress_percent,
            "processed_items": len(successful_chunks),
            "total_items": len(chunks),
            "result_data": job.result_data,
            "error_message": job.error_message,
            "message": "Processing complete!" if job.status == JobStatus.COMPLETED else "Processing failed",
        })

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
            job.status = JobStatus.FAILED
            job.error_message = str(e)
            job.completed_at = datetime.now(timezone.utc)
            db.commit()

            # Broadcast error via WebSocket
            await broadcast_job_status(job_uuid, {
                "job_id": job_id,
                "status": "failed",
                "error_message": str(e),
                "message": "Job failed due to an error",
            })

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
        subject = "✓ Document Processing Complete" if job.status == JobStatus.COMPLETED else "✗ Document Processing Failed"

        if job.status == JobStatus.COMPLETED:
            result = job.result_data or {}
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
    Check for trials expiring in 3 days and send reminder emails.

    Scheduled: Daily at 9:00 AM UTC

    Args:
        ctx: ARQ context

    Returns:
        Result dict with count of emails sent
    """
    from datetime import timedelta
    from sqlalchemy import select, and_
    from app.db.models.tenant import Tenant
    from app.services.email_notification_service import EmailNotificationService

    logger.info("Running trial expiration check (3-day reminder)")

    db = SessionLocal()
    emails_sent = 0

    try:
        # Find tenants with trials expiring in 3 days
        target_date = datetime.now(timezone.utc) + timedelta(days=3)
        date_start = target_date.replace(hour=0, minute=0, second=0, microsecond=0)
        date_end = target_date.replace(hour=23, minute=59, second=59, microsecond=999999)

        query = select(Tenant).where(
            and_(
                Tenant.subscription_status == "trialing",
                Tenant.trial_ends_at >= date_start,
                Tenant.trial_ends_at <= date_end,
            )
        )

        result = await db.execute(query)
        expiring_tenants = result.scalars().all()

        logger.info(f"Found {len(expiring_tenants)} tenants with trials expiring in 3 days")

        # Send reminder emails
        email_service = EmailNotificationService(db)

        for tenant in expiring_tenants:
            try:
                success = await email_service.send_trial_expiration_reminder(
                    tenant_id=tenant.id,
                    days_remaining=3
                )
                if success:
                    emails_sent += 1
                    logger.info(f"Sent trial expiration reminder to tenant {tenant.name}")
                else:
                    logger.warning(f"Failed to send reminder to tenant {tenant.name}")
            except Exception as e:
                logger.error(f"Error sending reminder to tenant {tenant.name}: {e}")

        logger.info(f"Trial expiration check complete. Sent {emails_sent}/{len(expiring_tenants)} emails")

        return {
            "status": "success",
            "emails_sent": emails_sent,
            "tenants_checked": len(expiring_tenants),
        }

    except Exception as e:
        logger.error(f"Error in trial expiration check: {e}")
        return {
            "status": "error",
            "error": str(e),
            "emails_sent": emails_sent,
        }
    finally:
        await db.close()


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
    from sqlalchemy import select, and_
    from app.db.models.payment import PaymentTransaction
    from app.services.email_notification_service import EmailNotificationService

    logger.info("Running subscription confirmation email job")

    db = SessionLocal()
    emails_sent = 0

    try:
        # Find successful payments in the last 10 minutes
        cutoff_time = datetime.now(timezone.utc) - timedelta(minutes=10)

        query = select(PaymentTransaction).where(
            and_(
                PaymentTransaction.status == "success",
                PaymentTransaction.created_at >= cutoff_time,
            )
        )

        result = await db.execute(query)
        recent_payments = result.scalars().all()

        logger.info(f"Found {len(recent_payments)} successful payments in last 10 minutes")

        # Send confirmation emails
        email_service = EmailNotificationService(db)

        for payment in recent_payments:
            try:
                success = await email_service.send_subscription_confirmation(
                    tenant_id=payment.tenant_id,
                    payment_id=payment.id
                )
                if success:
                    emails_sent += 1
                    logger.info(f"Sent subscription confirmation for payment {payment.id}")
            except Exception as e:
                logger.error(f"Error sending confirmation for payment {payment.id}: {e}")

        logger.info(f"Subscription confirmation job complete. Sent {emails_sent}/{len(recent_payments)} emails")

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
        await db.close()


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
    from sqlalchemy import select, and_
    from app.db.models.payment import PaymentTransaction
    from app.services.email_notification_service import EmailNotificationService

    logger.info("Running payment failure notification job")

    db = SessionLocal()
    emails_sent = 0

    try:
        # Find failed payments in the last 2 hours
        cutoff_time = datetime.now(timezone.utc) - timedelta(hours=2)

        query = select(PaymentTransaction).where(
            and_(
                PaymentTransaction.status == "failed",
                PaymentTransaction.created_at >= cutoff_time,
            )
        )

        result = await db.execute(query)
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
                    failure_reason=None  # Could extract from payment metadata
                )
                if success:
                    emails_sent += 1
                    logger.info(f"Sent payment failure notification for payment {payment.id}")
            except Exception as e:
                logger.error(f"Error sending failure notification for payment {payment.id}: {e}")

        logger.info(f"Payment failure job complete. Sent {emails_sent}/{len(failed_payments)} emails")

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
        await db.close()


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
    from sqlalchemy import select, func
    from app.db.models.tenant import Tenant
    from app.db.models.audit import AnalyticsMetric

    logger.info("Running daily metrics aggregation")

    db = SessionLocal()
    metrics_created = 0

    try:
        # Calculate yesterday's date range
        today = datetime.now(timezone.utc).date()
        yesterday = today - timedelta(days=1)
        period_start = datetime.combine(yesterday, datetime.min.time()).replace(tzinfo=timezone.utc)
        period_end = datetime.combine(yesterday, datetime.max.time()).replace(tzinfo=timezone.utc)

        # Aggregate tenant count by subscription tier
        tier_query = select(
            Tenant.subscription_tier,
            func.count(Tenant.id).label("count")
        ).where(
            Tenant.subscription_status == "active"
        ).group_by(Tenant.subscription_tier)

        tier_result = await db.execute(tier_query)
        tier_counts = tier_result.all()

        # Store tier metrics
        for tier, count in tier_counts:
            metric = AnalyticsMetric(
                tenant_id=None,  # Platform-wide metric
                metric_type=f"active_tenants_{tier}",
                metric_value=float(count),
                period_start=period_start,
                period_end=period_end,
                meta={"tier": tier}
            )
            db.add(metric)
            metrics_created += 1

        # Total active tenants
        total_active_query = select(func.count(Tenant.id)).where(
            Tenant.subscription_status == "active"
        )
        total_active_result = await db.execute(total_active_query)
        total_active = total_active_result.scalar()

        metric = AnalyticsMetric(
            tenant_id=None,
            metric_type="total_active_tenants",
            metric_value=float(total_active or 0),
            period_start=period_start,
            period_end=period_end,
            meta={}
        )
        db.add(metric)
        metrics_created += 1

        # Total trialing tenants
        total_trial_query = select(func.count(Tenant.id)).where(
            Tenant.subscription_status == "trialing"
        )
        total_trial_result = await db.execute(total_trial_query)
        total_trial = total_trial_result.scalar()

        metric = AnalyticsMetric(
            tenant_id=None,
            metric_type="total_trialing_tenants",
            metric_value=float(total_trial or 0),
            period_start=period_start,
            period_end=period_end,
            meta={}
        )
        db.add(metric)
        metrics_created += 1

        await db.commit()

        logger.info(f"Daily metrics aggregation complete. Created {metrics_created} metrics for {yesterday}")

        return {
            "status": "success",
            "metrics_created": metrics_created,
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(),
        }

    except Exception as e:
        logger.error(f"Error in daily metrics aggregation: {e}")
        await db.rollback()
        return {
            "status": "error",
            "error": str(e),
            "metrics_created": metrics_created,
        }
    finally:
        await db.close()


# ARQ worker settings
def get_redis_settings():
    """Get Redis settings from environment at runtime."""
    from arq.connections import RedisSettings as ARQRedisSettings
    # Get from environment (respects Docker overrides)
    redis_url = os.getenv('REDIS_URL', 'redis://localhost:6379/0')
    logger.info(f"Worker connecting to Redis: {redis_url}")
    return ARQRedisSettings.from_dsn(redis_url)


class WorkerSettings:
    """ARQ worker configuration."""

    # Import scheduled tasks
    from app.workers.conversation_auto_resolver import (
        auto_abandon_inactive_conversations,
        auto_resolve_stale_tickets,
    )

    functions = [
        process_document_ingestion,
        auto_abandon_inactive_conversations,
        auto_resolve_stale_tickets,
        check_trial_expiration_3days,
        send_subscription_confirmations,
        send_payment_failure_emails,
        aggregate_daily_metrics,
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
    from arq.cron import cron

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
        # Check trial expiration (3-day reminder) - daily at 9 AM UTC
        cron(
            check_trial_expiration_3days,
            hour=9,
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
        # Aggregate daily metrics - daily at 1 AM UTC
        cron(
            aggregate_daily_metrics,
            hour=1,
            minute=0,
            run_at_startup=False,
        ),
    ]
