"""Service for managing background processing jobs."""

from datetime import datetime
from uuid import UUID

from loguru import logger
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.db.models.job import JobStatus, JobType, ProcessingJob
from app.schemas.job import JobCreate, JobResponse, JobUpdate


class JobService:
    """Service for job management."""

    def __init__(self, db: Session):
        """Initialize job service with database session."""
        self.db = db

    def create_job(self, job_data: JobCreate) -> JobResponse:
        """
        Create a new processing job.

        Args:
            job_data: Job creation data

        Returns:
            Created job response
        """
        db_job = ProcessingJob(
            tenant_id=job_data.tenant_id,
            job_type=job_data.job_type,
            status=JobStatus.QUEUED,
            total_items=job_data.total_items,
            processed_items=0,
            failed_items=0,
            input_data=job_data.input_data,
            created_by=job_data.created_by,
        )

        self.db.add(db_job)
        self.db.commit()
        self.db.refresh(db_job)

        logger.info(f"Created job {db_job.id} ({db_job.job_type.value})")

        return JobResponse.model_validate(db_job)

    def get_job(self, job_id: UUID) -> JobResponse | None:
        """
        Get job by ID.

        Args:
            job_id: Job UUID

        Returns:
            Job response if found, None otherwise
        """
        stmt = select(ProcessingJob).where(ProcessingJob.id == job_id)
        job = self.db.execute(stmt).scalar_one_or_none()

        if not job:
            return None

        return JobResponse.model_validate(job)

    def update_job(self, job_id: UUID, job_data: JobUpdate) -> JobResponse:
        """
        Update job status and progress.

        Args:
            job_id: Job UUID
            job_data: Updated job data

        Returns:
            Updated job response

        Raises:
            ValueError: If job not found
        """
        stmt = select(ProcessingJob).where(ProcessingJob.id == job_id)
        job = self.db.execute(stmt).scalar_one_or_none()

        if not job:
            raise ValueError("Job not found")

        # Update fields
        update_data = job_data.model_dump(exclude_unset=True)
        for field, value in update_data.items():
            setattr(job, field, value)

        # Set timestamps based on status changes
        if job_data.status == JobStatus.PROCESSING and not job.started_at:
            job.started_at = datetime.utcnow()
        elif job_data.status in [JobStatus.COMPLETED, JobStatus.FAILED] and not job.completed_at:
            job.completed_at = datetime.utcnow()

        self.db.commit()
        self.db.refresh(job)

        return JobResponse.model_validate(job)

    def list_jobs(
        self,
        tenant_id: UUID | None = None,
        status: JobStatus | None = None,
        job_type: JobType | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[JobResponse]:
        """
        List jobs with optional filters.

        Args:
            tenant_id: Filter by tenant
            status: Filter by status
            job_type: Filter by job type
            limit: Maximum results
            offset: Offset for pagination

        Returns:
            List of job responses
        """
        stmt = select(ProcessingJob)

        # Apply filters
        filters = []
        if tenant_id:
            filters.append(ProcessingJob.tenant_id == tenant_id)
        if status:
            filters.append(ProcessingJob.status == status)
        if job_type:
            filters.append(ProcessingJob.job_type == job_type)

        if filters:
            stmt = stmt.where(and_(*filters))

        # Order by creation time (newest first)
        stmt = stmt.order_by(ProcessingJob.created_at.desc())

        # Pagination
        stmt = stmt.limit(limit).offset(offset)

        jobs = self.db.execute(stmt).scalars().all()

        return [JobResponse.model_validate(job) for job in jobs]

    def count_jobs(
        self,
        tenant_id: UUID | None = None,
        status: JobStatus | None = None,
        job_type: JobType | None = None,
    ) -> int:
        """
        Count jobs matching filters.

        Args:
            tenant_id: Filter by tenant
            status: Filter by status
            job_type: Filter by job type

        Returns:
            Number of matching jobs
        """
        from sqlalchemy import func

        stmt = select(func.count(ProcessingJob.id))

        # Apply filters
        filters = []
        if tenant_id:
            filters.append(ProcessingJob.tenant_id == tenant_id)
        if status:
            filters.append(ProcessingJob.status == status)
        if job_type:
            filters.append(ProcessingJob.job_type == job_type)

        if filters:
            stmt = stmt.where(and_(*filters))

        count = self.db.execute(stmt).scalar()

        return count or 0

    def cancel_job(self, job_id: UUID) -> JobResponse:
        """
        Cancel a queued or processing job.

        Args:
            job_id: Job UUID

        Returns:
            Updated job response

        Raises:
            ValueError: If job not found or already terminal
        """
        stmt = select(ProcessingJob).where(ProcessingJob.id == job_id)
        job = self.db.execute(stmt).scalar_one_or_none()

        if not job:
            raise ValueError("Job not found")

        if job.is_terminal:
            raise ValueError(f"Cannot cancel job in {job.status.value} state")

        job.status = JobStatus.CANCELLED
        job.completed_at = datetime.utcnow()

        self.db.commit()
        self.db.refresh(job)

        logger.info(f"Cancelled job {job_id}")

        return JobResponse.model_validate(job)

    def cleanup_old_jobs(self, days: int = 30) -> int:
        """
        Delete completed jobs older than specified days.

        Args:
            days: Number of days to keep jobs

        Returns:
            Number of jobs deleted
        """
        from datetime import timedelta

        cutoff_date = datetime.utcnow() - timedelta(days=days)

        stmt = select(ProcessingJob).where(
            and_(
                ProcessingJob.completed_at < cutoff_date,
                or_(
                    ProcessingJob.status == JobStatus.COMPLETED,
                    ProcessingJob.status == JobStatus.FAILED,
                    ProcessingJob.status == JobStatus.CANCELLED,
                ),
            )
        )

        jobs = self.db.execute(stmt).scalars().all()

        for job in jobs:
            self.db.delete(job)

        self.db.commit()

        count = len(jobs)
        logger.info(f"Cleaned up {count} old jobs (older than {days} days)")

        return count
