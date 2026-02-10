"""Job management and status endpoints with WebSocket support."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect, status
from loguru import logger
from sqlalchemy.orm import Session

from app.api.deps import get_current_tenant_admin, get_db, verify_tenant_access
from app.db.models.job import JobStatus, JobType
from app.schemas.job import JobListResponse, JobResponse
from app.schemas.user import UserResponse
from app.services.job_service import JobService
from app.utils.websocket_manager import websocket_manager

router = APIRouter()


@router.get(
    "/jobs/{job_id}",
    response_model=JobResponse,
    status_code=status.HTTP_200_OK,
    summary="Get job status",
    description="Get current status and progress of a background job",
)
def get_job_status(
    job_id: UUID,
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_tenant_admin),
):
    """
    Get job status and progress.

    Args:
        job_id: Job UUID
        db: Database session
        current_user: Current authenticated user

    Returns:
        Job response with status and progress

    Raises:
        HTTPException: If job not found or access denied
    """
    job_service = JobService(db)
    job = job_service.get_job(job_id)

    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    # Verify tenant access
    verify_tenant_access(job.tenant_id, current_user)

    return job


@router.websocket("/ws/jobs/{job_id}")
async def websocket_job_updates(
    websocket: WebSocket,
    job_id: UUID,
    db: Session = Depends(get_db),
):
    """
    WebSocket endpoint for real-time job progress updates.

    Client connects and receives updates whenever job status/progress changes.

    Example client usage:
    ```javascript
    const ws = new WebSocket('ws://localhost:8000/api/v1/ws/jobs/{job_id}');

    ws.onmessage = (event) => {
        const update = JSON.parse(event.data);
        console.log('Job progress:', update.progress_percent);

        if (update.status === 'completed') {
            console.log('Job done!', update.result_data);
            ws.close();
        }
    };
    ```

    Args:
        websocket: WebSocket connection
        job_id: Job UUID to subscribe to
        db: Database session
    """
    try:
        # Connect WebSocket
        await websocket_manager.connect(websocket, job_id)

        # Send initial job status
        job_service = JobService(db)
        job = job_service.get_job(job_id)

        if not job:
            await websocket.send_json({"error": "Job not found", "job_id": str(job_id)})
            await websocket.close()
            return

        # Send current job state
        await websocket.send_json(
            {
                "job_id": str(job.id),
                "status": job.status.value,
                "progress_percent": job.progress_percent,
                "processed_items": job.processed_items,
                "total_items": job.total_items,
                "result_data": job.result_data,
                "error_message": job.error_message,
            }
        )

        # Subscribe to Redis updates and forward to WebSocket
        import asyncio

        async def redis_listener():
            """Listen to Redis pub/sub and forward updates to WebSocket."""
            try:
                async for update in websocket_manager.subscribe_to_job_updates(job_id):
                    await websocket.send_json(update)

                    # Close connection after job completes or fails
                    if update.get("status") in ["completed", "failed"]:
                        logger.info(f"Job {job_id} finished, closing WebSocket")
                        break
            except Exception as e:
                logger.error(f"Redis listener error: {e}")

        async def websocket_receiver():
            """Receive messages from WebSocket (for keepalive)."""
            try:
                while True:
                    await websocket.receive_text()
                    # Echo back for keepalive
                    await websocket.send_json({"type": "pong"})
            except WebSocketDisconnect:
                logger.info(f"WebSocket disconnected for job {job_id}")

        # Run both tasks concurrently
        await asyncio.gather(redis_listener(), websocket_receiver(), return_exceptions=True)

    except Exception as e:
        logger.error(f"WebSocket error for job {job_id}: {e}")
    finally:
        websocket_manager.disconnect(websocket, job_id)


@router.get(
    "/tenants/{tenant_id}/jobs",
    response_model=JobListResponse,
    status_code=status.HTTP_200_OK,
    summary="List jobs for tenant",
    description="List all background jobs for a tenant with optional filters",
)
def list_tenant_jobs(
    tenant_id: UUID,
    status_filter: JobStatus | None = Query(
        None, alias="status", description="Filter by job status"
    ),
    job_type: JobType | None = Query(None, description="Filter by job type"),
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(50, ge=1, le=100, description="Items per page"),
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_tenant_admin),
):
    """
    List all jobs for a tenant with pagination and filters.

    Args:
        tenant_id: Tenant UUID
        status_filter: Optional status filter
        job_type: Optional job type filter
        page: Page number (1-indexed)
        page_size: Items per page
        db: Database session
        current_user: Current authenticated user

    Returns:
        Paginated list of jobs

    Raises:
        HTTPException: If access denied
    """
    # Verify tenant access
    verify_tenant_access(tenant_id, current_user)

    job_service = JobService(db)

    # Get jobs
    offset = (page - 1) * page_size
    jobs = job_service.list_jobs(
        tenant_id=tenant_id,
        status=status_filter,
        job_type=job_type,
        limit=page_size,
        offset=offset,
    )

    # Get total count
    total = job_service.count_jobs(
        tenant_id=tenant_id,
        status=status_filter,
        job_type=job_type,
    )

    return JobListResponse(
        jobs=jobs,
        total=total,
        page=page,
        page_size=page_size,
    )


@router.delete(
    "/jobs/{job_id}",
    status_code=status.HTTP_200_OK,
    summary="Cancel job",
    description="Cancel a queued or running job",
)
def cancel_job(
    job_id: UUID,
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_tenant_admin),
):
    """
    Cancel a job that is queued or processing.

    Args:
        job_id: Job UUID
        db: Database session
        current_user: Current authenticated user

    Returns:
        Success message with updated job status

    Raises:
        HTTPException: If job not found, access denied, or already completed
    """
    job_service = JobService(db)

    # Get job to verify access
    job = job_service.get_job(job_id)
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    # Verify tenant access
    verify_tenant_access(job.tenant_id, current_user)

    try:
        cancelled_job = job_service.cancel_job(job_id)

        # Broadcast cancellation via WebSocket
        import asyncio

        asyncio.create_task(
            websocket_manager.broadcast_job_update(
                job_id,
                {
                    "job_id": str(job_id),
                    "status": "cancelled",
                    "message": "Job cancelled by user",
                },
            )
        )

        return {
            "message": "Job cancelled successfully",
            "job": cancelled_job,
        }

    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
