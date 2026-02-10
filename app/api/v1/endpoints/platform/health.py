"""Platform system health monitoring API endpoints."""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_platform_admin, get_db
from app.schemas.platform_admin import PlatformAdminResponse
from app.services.system_health_service import SystemHealthService

router = APIRouter()


@router.get("/jobs", status_code=status.HTTP_200_OK)
def get_job_health(
    hours: int = Query(24, ge=1, le=168, description="Hours to analyze"),
    db: Session = Depends(get_db),
    current_admin: PlatformAdminResponse = Depends(get_current_platform_admin),
):
    """
    Get background job health and statistics.

    Returns:
        Job statistics including success rate, failures, and health status
    """
    try:
        health_service = SystemHealthService(db)
        metrics = health_service.get_job_status(hours=hours)

        return metrics

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch job health: {str(e)}",
        ) from e


@router.get("/api", status_code=status.HTTP_200_OK)
def get_api_health(
    hours: int = Query(1, ge=1, le=24, description="Hours to analyze"),
    db: Session = Depends(get_db),
    current_admin: PlatformAdminResponse = Depends(get_current_platform_admin),
):
    """
    Get API health metrics from audit logs.

    Returns:
        API performance metrics including request rates and error rates
    """
    try:
        health_service = SystemHealthService(db)
        metrics = health_service.get_api_health(hours=hours)

        return metrics

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch API health: {str(e)}",
        ) from e


@router.get("/database", status_code=status.HTTP_200_OK)
def get_database_health(
    db: Session = Depends(get_db),
    current_admin: PlatformAdminResponse = Depends(get_current_platform_admin),
):
    """
    Get database performance metrics.

    Returns:
        Database health including connection pool stats and response times
    """
    try:
        health_service = SystemHealthService(db)
        metrics = health_service.get_database_health()

        return metrics

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch database health: {str(e)}",
        ) from e


@router.get("/queue", status_code=status.HTTP_200_OK)
def get_queue_health(
    db: Session = Depends(get_db),
    current_admin: PlatformAdminResponse = Depends(get_current_platform_admin),
):
    """
    Get Redis/ARQ queue health metrics.

    Returns:
        Queue statistics including backlog and memory usage
    """
    try:
        health_service = SystemHealthService(db)
        metrics = health_service.get_queue_status()

        return metrics

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch queue health: {str(e)}",
        ) from e


@router.get("/overall", status_code=status.HTTP_200_OK)
def get_overall_health(
    db: Session = Depends(get_db),
    current_admin: PlatformAdminResponse = Depends(get_current_platform_admin),
):
    """
    Get comprehensive system health summary.

    Returns:
        Complete health status of all system components
    """
    try:
        health_service = SystemHealthService(db)
        summary = health_service.get_overall_health()

        return summary

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch overall health: {str(e)}",
        ) from e
