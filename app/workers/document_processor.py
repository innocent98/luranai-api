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
    ]
