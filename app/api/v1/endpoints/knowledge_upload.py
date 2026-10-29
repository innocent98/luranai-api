"""File upload endpoint for knowledge documents."""

import io
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, UploadFile, status
from loguru import logger
from pydantic import HttpUrl
from sqlalchemy.orm import Session

from app.api.deps import (
    get_current_tenant_admin,
    get_db,
    verify_subscription_active,
    verify_tenant_access,
)
from app.core.config import settings
from app.db.models.job import JobStatus, JobType
from app.db.models.knowledge import SourceType
from app.schemas.job import DocumentUploadJobResponse, JobCreate
from app.schemas.user import UserResponse
from app.services.job_service import JobService
from app.services.knowledge_service import KnowledgeService
from app.services.url_scraper_service import URLScraperService
from app.utils.http_rate_limiter import upload_rate_limit
from app.utils.knowledge_failures import FailureCode, KnowledgeError

router = APIRouter()


# Supported file types
SUPPORTED_EXTENSIONS = {
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}

MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB


def extract_text_from_file(file: UploadFile) -> str:
    """
    Extract text from uploaded file based on type.

    Args:
        file: Uploaded file

    Returns:
        Extracted text content

    Raises:
        HTTPException: If file type not supported or extraction fails
    """
    filename = (file.filename or "").lower()

    # Text files
    if filename.endswith((".txt", ".md")):
        content = file.file.read()
        try:
            return content.decode("utf-8")
        except UnicodeDecodeError:
            # Try other encodings
            try:
                return content.decode("latin-1")
            except Exception as e:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Could not decode text file: {str(e)}",
                ) from e

    # PDF files
    elif filename.endswith(".pdf"):
        try:
            import PyPDF2

            pdf_reader = PyPDF2.PdfReader(io.BytesIO(file.file.read()))
            text_parts = []
            for page in pdf_reader.pages:
                text_parts.append(page.extract_text())
            return "\n\n".join(text_parts)
        except ImportError as err:
            raise HTTPException(
                status_code=status.HTTP_501_NOT_IMPLEMENTED,
                detail="PDF support not installed. Install PyPDF2: pip install PyPDF2",
            ) from err
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Could not extract text from PDF: {str(e)}",
            ) from e

    # DOCX files
    elif filename.endswith(".docx"):
        try:
            import docx

            doc = docx.Document(io.BytesIO(file.file.read()))
            text_parts = [paragraph.text for paragraph in doc.paragraphs]
            return "\n\n".join(text_parts)
        except ImportError as err:
            raise HTTPException(
                status_code=status.HTTP_501_NOT_IMPLEMENTED,
                detail="DOCX support not installed. Install python-docx: pip install python-docx",
            ) from err
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Could not extract text from DOCX: {str(e)}",
            ) from e

    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported file type. Supported: {', '.join(SUPPORTED_EXTENSIONS.keys())}",
        )


@router.post(
    "/upload-file",
    response_model=DocumentUploadJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Upload knowledge document (file)",
    description="""
    Upload a document file for background processing.

    Supported formats: .txt, .md, .pdf, .docx

    The file will be queued for processing:
    1. Text extracted based on file type
    2. Chunked into segments
    3. Embedded using Google AI (rate-limited at 10 RPM)
    4. Stored in PostgreSQL and Qdrant

    Returns immediately with job ID. Check status via:
    - GET /api/v1/jobs/{job_id} (REST polling)
    - WS /api/v1/ws/jobs/{job_id} (WebSocket real-time)
    """,
    dependencies=[Depends(upload_rate_limit)],
)
async def upload_knowledge_file(
    file: UploadFile = File(..., description="Document file to upload"),
    tenant_id: UUID = Form(..., description="Tenant ID"),
    source_type: SourceType = Form(default=SourceType.HELP_DOC, description="Type of document"),
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_tenant_admin),
    _subscription_check: None = Depends(verify_subscription_active),
):
    """
    Upload a knowledge document file.

    This endpoint accepts file uploads and processes them through the full
    ingestion pipeline (chunking, embedding, storage).

    Args:
        file: Uploaded file (.txt, .md, .pdf, .docx)
        tenant_id: Tenant UUID
        source_type: Document type (FAQ, help_doc, policy, etc.)
        db: Database session
        current_user: Current authenticated user

    Returns:
        Created knowledge source with metadata

    Raises:
        HTTPException: If file is invalid, too large, or processing fails
    """
    # Verify tenant access
    verify_tenant_access(tenant_id, current_user)

    # Validate file
    if not file.filename:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No filename provided")

    # Check file size
    file.file.seek(0, 2)  # Seek to end
    file_size = file.file.tell()
    file.file.seek(0)  # Reset to start

    if file_size > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File too large. Maximum size: {MAX_FILE_SIZE / 1024 / 1024}MB",
        )

    if file_size == 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="File is empty")

    logger.info(f"Processing file upload: {file.filename} ({file_size} bytes)")

    # Check knowledge source limits
    knowledge_service = KnowledgeService(db)
    knowledge_service.check_knowledge_source_limit(tenant_id, source_category="file")

    # Check storage limit
    from app.services.tenant_service import TenantService

    tenant_service = TenantService(db)

    current_mb = knowledge_service.calculate_tenant_storage_mb(tenant_id)
    file_mb = file_size / 1024 / 1024

    capabilities = tenant_service.get_capabilities(tenant_id)
    if (
        capabilities
        and capabilities.max_knowledge_mb is not None
        and current_mb + file_mb > capabilities.max_knowledge_mb
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "storage_limit_exceeded",
                "message": f"Storage limit exceeded ({current_mb:.1f}/{capabilities.max_knowledge_mb} MB).",
                "current_mb": round(current_mb, 2),
                "limit_mb": capabilities.max_knowledge_mb,
                "file_mb": round(file_mb, 2),
                "upgrade_message": "Upgrade to a higher tier for more storage.",
            },
        )

    try:
        # Extract text from file
        logger.info(f"Extracting text from {file.filename}...")
        document_text = extract_text_from_file(file)

        # Nothing readable at all (e.g. a scanned PDF): extraction is synchronous here, so say so
        # now. Text that is readable but too short is judged by the worker and surfaces as
        # `failure_reason: document_too_short` on the job.
        if not any(ch.isalnum() for ch in (document_text or "")):
            raise KnowledgeError(FailureCode.NO_TEXT_EXTRACTED)

        logger.info(f"Extracted {len(document_text)} characters")

        # Estimate chunks for progress tracking
        estimated_chunks = max(1, len(document_text) // 1600)  # ~400 tokens = 1600 chars
        estimated_time_seconds = estimated_chunks * 6  # 6 seconds per chunk (10 RPM rate limit)

        # Create job record
        job_service = JobService(db)
        job = job_service.create_job(
            JobCreate(
                tenant_id=tenant_id,
                job_type=JobType.DOCUMENT_INGESTION,
                total_items=None,  # Will be set after chunking
                input_data={
                    "tenant_id": str(tenant_id),
                    "document_text": document_text,
                    "document_name": file.filename,
                    "source_type": source_type.value,
                    "source_category": "file",  # NEW
                    "file_size_bytes": file_size,  # NEW
                    "metadata": {
                        "original_filename": file.filename,
                        "file_size": file_size,
                        "content_type": file.content_type,
                        "uploaded_by": str(current_user.id),
                    },
                    "uploaded_by": str(current_user.id),
                },
                created_by=current_user.id,
            )
        )

        # Queue job for background processing
        from arq import create_pool
        from arq.connections import RedisSettings

        try:
            redis = await create_pool(RedisSettings.from_dsn(settings.REDIS_URL))
            await redis.enqueue_job(
                "process_document_ingestion",
                str(job.id),
            )
            logger.info(f"Queued job {job.id} for document: {file.filename}")
        except Exception as e:
            logger.error(f"Failed to queue job: {e}")
            # Mark job as failed
            from app.schemas.job import JobUpdate

            job_service.update_job(
                job.id,
                JobUpdate(status=JobStatus.FAILED, error_message=f"Failed to queue job: {str(e)}"),
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to queue document for processing: {str(e)}",
            ) from e

        # Return 202 Accepted with job details
        return DocumentUploadJobResponse(
            job_id=job.id,
            status=job.status,
            message="Document upload queued for processing. Check status via provided URL or connect to WebSocket.",
            check_status_url=f"{settings.API_V1_STR}/jobs/{job.id}",
            estimated_chunks=estimated_chunks,
            estimated_time_seconds=estimated_time_seconds,
        )

    except (HTTPException, KnowledgeError):
        raise
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"Document processing failed: {str(e)}"
        ) from e
    except Exception as e:
        logger.error(f"Error processing file upload: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Internal error during file processing: {str(e)}",
        ) from e


@router.post(
    "/upload-url",
    response_model=DocumentUploadJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Upload knowledge from webpage URL",
    description="""
    Scrape and process webpage content as knowledge source.

    The URL will be:
    1. Scraped and text content extracted
    2. Chunked into segments
    3. Embedded using Google AI
    4. Stored in PostgreSQL and Qdrant

    Returns immediately with job ID. Check status via:
    - GET /api/v1/jobs/{job_id} (REST polling)
    - WS /api/v1/ws/jobs/{job_id} (WebSocket real-time)
    """,
    dependencies=[Depends(upload_rate_limit)],
)
async def upload_url_source(
    url: HttpUrl = Body(..., description="Webpage URL to scrape"),
    tenant_id: UUID = Body(..., description="Tenant ID"),
    title: str | None = Body(None, description="Custom title (uses page title if not provided)"),
    source_type: SourceType = Body(default=SourceType.OTHER, description="Type of document"),
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_tenant_admin),
    _subscription_check: None = Depends(verify_subscription_active),
):
    """
    Add webpage URL as knowledge source.

    Scrapes the URL, extracts text content, and processes it like a file upload.

    Args:
        url: Webpage URL to scrape
        tenant_id: Tenant UUID
        title: Optional custom title
        source_type: Document type
        db: Database session
        current_user: Current authenticated user
        _subscription_check: Subscription verification

    Returns:
        Created job response

    Raises:
        HTTPException: If scraping fails or limits exceeded
    """
    # Verify tenant access
    verify_tenant_access(tenant_id, current_user)

    # Check knowledge source limits
    knowledge_service = KnowledgeService(db)
    knowledge_service.check_knowledge_source_limit(tenant_id, source_category="url")

    # Scrape URL
    scraper = URLScraperService()
    scraped_data = await scraper.scrape_url(str(url))

    logger.info(f"Scraped URL {url}: {scraped_data['content_length']} characters")

    # Check storage limit
    from app.services.tenant_service import TenantService

    tenant_service = TenantService(db)

    content_mb = len(scraped_data["content"].encode("utf-8")) / 1024 / 1024
    current_mb = knowledge_service.calculate_tenant_storage_mb(tenant_id)

    capabilities = tenant_service.get_capabilities(tenant_id)
    if (
        capabilities
        and capabilities.max_knowledge_mb is not None
        and current_mb + content_mb > capabilities.max_knowledge_mb
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "storage_limit_exceeded",
                "message": f"Storage limit exceeded ({current_mb:.1f}/{capabilities.max_knowledge_mb} MB).",
                "current_mb": round(current_mb, 2),
                "limit_mb": capabilities.max_knowledge_mb,
                "content_mb": round(content_mb, 2),
            },
        )

    # Estimate chunks for progress tracking
    estimated_chunks = max(1, len(scraped_data["content"]) // 1600)
    estimated_time_seconds = estimated_chunks * 6  # 6 seconds per chunk

    # Create job record
    job_service = JobService(db)
    job = job_service.create_job(
        JobCreate(
            tenant_id=tenant_id,
            job_type=JobType.DOCUMENT_INGESTION,
            total_items=None,  # Will be set after chunking
            input_data={
                "tenant_id": str(tenant_id),
                "document_text": scraped_data["content"],
                "document_name": title or scraped_data["title"],
                "source_type": source_type.value,
                "source_category": "url",
                "source_url": scraped_data["url"],
                "file_size_bytes": len(scraped_data["content"].encode("utf-8")),
                "metadata": {
                    "original_url": str(url),
                    "final_url": scraped_data["url"],
                    "content_length": scraped_data["content_length"],
                    "scraped_at": datetime.utcnow().isoformat(),
                    "uploaded_by": str(current_user.id),
                },
                "uploaded_by": str(current_user.id),
            },
            created_by=current_user.id,
        )
    )

    # Queue job for background processing
    try:
        from arq import create_pool
        from arq.connections import RedisSettings

        redis = await create_pool(RedisSettings.from_dsn(settings.REDIS_URL))
        await redis.enqueue_job(
            "process_document_ingestion",
            str(job.id),
        )
        logger.info(f"Queued job {job.id} for URL: {url}")
    except Exception as e:
        logger.error(f"Failed to queue job: {e}")
        # Mark job as failed
        from app.schemas.job import JobUpdate

        job_service.update_job(
            job.id,
            JobUpdate(status=JobStatus.FAILED, error_message=f"Failed to queue job: {str(e)}"),
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to queue URL for processing: {str(e)}",
        ) from e

    # Return 202 Accepted with job details
    return DocumentUploadJobResponse(
        job_id=job.id,
        status=job.status,
        message="URL scraping queued for processing. Check status via provided URL or connect to WebSocket.",
        check_status_url=f"{settings.API_V1_STR}/jobs/{job.id}",
        estimated_chunks=estimated_chunks,
        estimated_time_seconds=estimated_time_seconds,
    )
