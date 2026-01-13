# Queue-Based Document Processing Implementation

## Overview

This document outlines the implementation of a **robust, production-ready queue-based system** for processing document uploads asynchronously with rate limiting.

### Why Queue-Based Processing?

1. ✅ **Non-blocking** - Users get immediate response, don't wait for processing
2. ✅ **Rate limit safe** - Processes embeddings at 10 RPM (Google API free tier: 15 RPM)
3. ✅ **Concurrent uploads** - Multiple users can upload simultaneously
4. ✅ **Fault tolerant** - Automatic retries on failures
5. ✅ **Scalable** - Can add more workers as needed
6. ✅ **Professional UX** - Like Vercel/Render deployments

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        UPLOAD FLOW                               │
└─────────────────────────────────────────────────────────────────┘

[User] uploads document via API
    ↓
[FastAPI Endpoint] /api/v1/knowledge/upload-file
    ├─ Validates file
    ├─ Creates ProcessingJob record
    ├─ Queues job in Redis (ARQ)
    └─ Returns 202 Accepted with job_id
    ↓
[Response to User]
{
  "job_id": "uuid",
  "status": "queued",
  "check_status_url": "/api/v1/jobs/{job_id}",
  "message": "Upload queued for processing"
}

┌─────────────────────────────────────────────────────────────────┐
│                    BACKGROUND PROCESSING                         │
└─────────────────────────────────────────────────────────────────┘

[ARQ Worker] (separate process) picks up job
    ↓
[Rate Limiter] Ensures max 10 API calls/minute
    ↓
[Document Processor]
    ├─ Step 1: Create source + chunks (no API calls)
    ├─ Step 2: For each chunk:
    │   ├─ Wait for rate limiter → acquire()
    │   ├─ Call Google Embedding API
    │   ├─ Store vector in Qdrant
    │   └─ Update job progress
    └─ Step 3: Mark source ACTIVE / FAILED
    ↓
[Email Service] Send completion notification
    ↓
[Job Status] Updated to COMPLETED / FAILED

┌─────────────────────────────────────────────────────────────────┐
│                    STATUS CHECKING                               │
└─────────────────────────────────────────────────────────────────┘

[Frontend] Polls GET /api/v1/jobs/{job_id} every 5 seconds
    ↓
[Response]
{
  "status": "processing",
  "progress_percent": 45,
  "processed_items": 22,
  "total_items": 50,
  "message": "Embedding chunk 22/50..."
}

[When Complete]
{
  "status": "completed",
  "progress_percent": 100,
  "result_data": {
    "source_id": "uuid",
    "chunks_created": 50
  }
}
```

---

## Components Implemented

### ✅ 1. Database Model - `ProcessingJob`

**File:** `app/db/models/job.py`

```python
class ProcessingJob:
    id: UUID
    tenant_id: UUID
    job_type: JobType  # DOCUMENT_INGESTION, BULK_EMBEDDING, etc.
    status: JobStatus  # QUEUED, PROCESSING, COMPLETED, FAILED

    # Progress tracking
    total_items: int  # Total chunks
    processed_items: int  # Chunks completed
    failed_items: int  # Chunks that failed

    # Data
    input_data: JSON  # {document_text, document_name, ...}
    result_data: JSON  # {source_id, chunks_created, ...}
    error_message: str  # If failed

    # Timing
    created_at: datetime
    started_at: datetime
    completed_at: datetime

    # Relationships
    tenant: Tenant
    created_by: User
```

**Features:**
- Tracks complete job lifecycle
- Progress reporting (percent complete)
- Error details for troubleshooting
- Terminal state checking (completed/failed/cancelled)

---

### ✅ 2. Pydantic Schemas - Job Responses

**File:** `app/schemas/job.py`

```python
class JobResponse:
    """Full job details"""
    id: UUID
    status: JobStatus
    progress_percent: int  # 0-100
    total_items: int
    processed_items: int
    result_data: dict
    error_message: str
    created_at: datetime

class DocumentUploadJobResponse:
    """Immediate response after upload"""
    job_id: UUID
    status: "queued"
    check_status_url: str
    estimated_chunks: int
    estimated_time_seconds: int
```

---

### ✅ 3. Background Worker - ARQ Task Queue

**File:** `app/workers/document_processor.py`

**Key Functions:**

#### `process_document_ingestion(ctx, job_id)`
Main worker function that:
1. Fetches job from database
2. Marks job as PROCESSING
3. Creates source + chunks (fast, no API)
4. Loops through chunks with rate limiting:
   - Waits for rate limiter
   - Generates embedding
   - Stores in Qdrant
   - Updates progress
5. Marks source ACTIVE/FAILED
6. Sends email notification
7. Returns result

**Rate Limiter:**
```python
class EmbeddingRateLimiter:
    """Ensures max 10 calls/minute (safe buffer from 15 RPM limit)"""

    async def acquire(self):
        # Tracks API call times
        # Sleeps if limit reached
        # Returns when safe to proceed
```

**Worker Settings:**
```python
class WorkerSettings:
    functions = [process_document_ingestion]
    redis_settings = settings.REDIS_URL
    max_jobs = 5  # Process 5 jobs concurrently
    job_timeout = 3600  # 1 hour max per job
    max_tries = 3  # Retry failed jobs 3 times
```

---

### ✅ 4. Job Service - Database Operations

**File:** `app/services/job_service.py`

**Methods:**
- `create_job()` - Create new job record
- `get_job(job_id)` - Get job status
- `update_job()` - Update progress
- `list_jobs()` - List jobs with filters
- `cancel_job()` - Cancel running job
- `cleanup_old_jobs(days=30)` - Delete old completed jobs

---

### ✅ 5. Knowledge Service - Prepare Chunks

**File:** `app/services/knowledge_service.py`

**New Method:**
```python
async def create_source_and_chunks(
    tenant_id, document_text, document_name, ...
) -> KnowledgeSourceResponse:
    """
    Create source and chunks WITHOUT embeddings.

    Used by background worker to:
    1. Hash content
    2. Create source (status: PROCESSING)
    3. Chunk text
    4. Create chunk records (vector_id=null)

    Worker then fills in embeddings later.
    """
```

---

## Remaining Implementation Steps

### 🔲 6. Update Upload Endpoints

**File:** `app/api/v1/endpoints/knowledge_upload.py`

**Changes Needed:**
```python
@router.post("/upload-file")
async def upload_knowledge_file(...):
    # OLD: await knowledge_service.process_document(...)

    # NEW:
    # 1. Create job record
    job_service = JobService(db)
    job = job_service.create_job(JobCreate(
        tenant_id=tenant_id,
        job_type=JobType.DOCUMENT_INGESTION,
        input_data={
            "document_text": document_text,
            "document_name": file.filename,
            "source_type": source_type,
            ...
        },
        created_by=current_user.id,
    ))

    # 2. Queue for background processing
    from arq import create_pool
    redis = await create_pool(settings.REDIS_URL)
    await redis.enqueue_job(
        'process_document_ingestion',
        str(job.id)
    )

    # 3. Return 202 Accepted
    return Response(
        status_code=202,
        content=DocumentUploadJobResponse(
            job_id=job.id,
            status=JobStatus.QUEUED,
            check_status_url=f"/api/v1/jobs/{job.id}",
            estimated_chunks=len(document_text) // 1600,
            estimated_time_seconds=(len(document_text) // 1600) * 6,
        ).model_dump_json()
    )
```

---

### 🔲 7. Job Status API Endpoints

**File:** `app/api/v1/endpoints/jobs.py` (NEW)

**Endpoints to Create:**

```python
@router.get("/jobs/{job_id}")
def get_job_status(job_id: UUID, ...):
    """Get current job status and progress."""
    return job_service.get_job(job_id)

@router.get("/tenants/{tenant_id}/jobs")
def list_tenant_jobs(tenant_id: UUID, status: Optional[JobStatus] = None, ...):
    """List all jobs for a tenant."""
    return job_service.list_jobs(tenant_id=tenant_id, status=status)

@router.delete("/jobs/{job_id}")
def cancel_job(job_id: UUID, ...):
    """Cancel a running job."""
    return job_service.cancel_job(job_id)
```

---

### 🔲 8. Email Notifications

**File:** `app/utils/email.py` (UPDATE)

**Add Function:**
```python
async def send_email(email_to: str, subject: str, body: str):
    """Send email notification."""
    if not settings.SMTP_HOST:
        logger.debug("Email not configured")
        return

    # Use existing emails library
    from emails import Message

    message = Message(
        subject=subject,
        html=body,
        mail_from=(settings.EMAILS_FROM_NAME, settings.EMAILS_FROM_EMAIL)
    )

    response = message.send(
        to=email_to,
        smtp={
            'host': settings.SMTP_HOST,
            'port': settings.SMTP_PORT,
            'tls': settings.SMTP_TLS,
            'user': settings.SMTP_USER,
            'password': settings.SMTP_PASSWORD,
        }
    )

    return response.status_code == 250
```

---

### 🔲 9. Database Migration

**Command:**
```bash
# Generate migration
poetry run alembic revision --autogenerate -m "Add processing_jobs table"

# Apply migration
poetry run alembic upgrade head
```

**Verify:**
```sql
-- Check table created
SELECT * FROM processing_jobs LIMIT 5;
```

---

### 🔲 10. Worker Deployment

**Start Worker Process:**

**File:** `worker.py` (NEW - project root)
```python
"""ARQ worker startup script."""

from arq import run_worker
from app.workers.document_processor import WorkerSettings

if __name__ == '__main__':
    run_worker(WorkerSettings)
```

**Update `docker-compose.yml`:**
```yaml
services:
  worker:
    build: .
    command: poetry run python worker.py
    environment:
      - DATABASE_URL=${DATABASE_URL}
      - REDIS_URL=${REDIS_URL}
      - GOOGLE_API_KEY=${GOOGLE_API_KEY}
      - QDRANT_URL=${QDRANT_URL}
    depends_on:
      - db
      - redis
      - qdrant
    restart: unless-stopped
    networks:
      - luran_network
```

**Start Worker:**
```bash
# Development (local)
poetry run python worker.py

# Production (Docker)
docker-compose up -d worker
```

---

## Testing Workflow

### 1. Start All Services
```bash
docker-compose up -d  # API, DB, Redis, Qdrant, Worker
```

### 2. Upload Document
```bash
curl -X POST http://localhost:8000/api/v1/knowledge/upload-file \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@document.pdf" \
  -F "tenant_id=bb24bf9e-63c2-4497-b5f5-e46dd916f7ea" \
  -F "source_type=faq"

# Response (202 Accepted):
{
  "job_id": "abc-123-def",
  "status": "queued",
  "check_status_url": "/api/v1/jobs/abc-123-def",
  "estimated_time_seconds": 180
}
```

### 3. Check Status
```bash
# Poll every 5 seconds
watch -n 5 'curl http://localhost:8000/api/v1/jobs/abc-123-def \
  -H "Authorization: Bearer $TOKEN"'

# Response (processing):
{
  "status": "processing",
  "progress_percent": 45,
  "processed_items": 22,
  "total_items": 50
}

# Response (completed):
{
  "status": "completed",
  "progress_percent": 100,
  "result_data": {
    "source_id": "source-uuid",
    "chunks_created": 50
  }
}
```

### 4. Verify Email Sent
Check inbox for completion notification.

### 5. Query Uploaded Knowledge
```bash
curl -X POST http://localhost:8000/api/v1/chat \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"tenant_id": "...", "message": "What is covered?"}'
```

---

## Monitoring & Troubleshooting

### Check Worker Status
```bash
# View worker logs
docker logs -f luran_worker

# Check Redis queue
redis-cli
> KEYS arq:*
> LLEN arq:queue:default
```

### Check Job Status in Database
```sql
SELECT id, status, progress_percent, error_message
FROM processing_jobs
WHERE tenant_id = 'tenant-uuid'
ORDER BY created_at DESC
LIMIT 10;
```

### Common Issues

**1. Worker Not Processing Jobs**
- Check: Is worker running? `docker ps | grep worker`
- Check: Redis connection? `redis-cli ping`
- Check: Worker logs? `docker logs luran_worker`

**2. Jobs Stuck in QUEUED**
- Restart worker: `docker restart luran_worker`
- Check for errors in worker logs

**3. Rate Limit Still Hit**
- Reduce rate limiter to 8 RPM (more conservative)
- Check if multiple tenants uploading simultaneously

**4. Email Not Sending**
- Check: SMTP settings in `.env`
- Check: Worker has access to email service

---

## Performance Characteristics

### Upload Speed
- Small document (10 chunks): ~60 seconds
- Medium document (30 chunks): ~3 minutes
- Large document (100 chunks): ~10 minutes

### Throughput
- Single worker: ~100 chunks/10 minutes
- With 3 workers: ~300 chunks/10 minutes
- With paid API tier (60 RPM): 6x faster

### Resource Usage
- Worker memory: ~100-200MB per process
- Redis memory: ~10MB per 1000 jobs
- Database: ~1KB per job record

---

## Production Recommendations

1. **Monitor Queue Size**
   - Alert if queue > 100 jobs (may need more workers)

2. **Set Job Timeout**
   - Current: 1 hour
   - Adjust based on max document size

3. **Cleanup Old Jobs**
   - Run daily: `job_service.cleanup_old_jobs(days=30)`

4. **Email Throttling**
   - Rate limit emails to avoid spam filters

5. **Upgrade to Paid Tier**
   - When exceeding 1,000 uploads/day
   - Cost: ~$1-2/month for typical usage

---

## Dependencies Added

```toml
# pyproject.toml
arq = "^0.26.0"  # Async task queue
```

**Install:**
```bash
poetry add arq
poetry install
```

---

## Summary

✅ **Benefits of Queue-Based System:**
- Non-blocking uploads (instant response)
- Rate limit safe (10 RPM with buffer)
- Handles concurrent uploads
- Professional UX (like Vercel/Render)
- Fault tolerant (automatic retries)
- Scalable (add more workers)
- Email notifications
- Progress tracking

🎯 **Remaining Work:**
- Update upload endpoints (return 202 Accepted)
- Create job status API endpoints
- Run database migration
- Add worker to docker-compose
- Test end-to-end workflow

**Estimated Time:** 2-3 hours to complete remaining steps.
