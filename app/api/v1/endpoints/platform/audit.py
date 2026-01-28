"""Platform audit log management API endpoints."""

from datetime import datetime
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status, Query, Response
from sqlalchemy.orm import Session

from app.api.deps import get_current_platform_admin, get_db
from app.utils.http_rate_limiter import platform_admin_rate_limit
from app.services.enhanced_audit_service import EnhancedAuditService
from app.schemas.platform_admin import PlatformAdminResponse

router = APIRouter()


@router.get("/logs", status_code=status.HTTP_200_OK, dependencies=[Depends(platform_admin_rate_limit)])
def search_audit_logs(
    tenant_id: Optional[str] = Query(None, description="Tenant ID filter"),
    user_id: Optional[str] = Query(None, description="User ID filter"),
    action: Optional[str] = Query(None, description="Action filter (partial match)"),
    resource_type: Optional[str] = Query(None, description="Resource type filter (partial match)"),
    start_date: Optional[str] = Query(None, description="Start date (ISO format)"),
    end_date: Optional[str] = Query(None, description="End date (ISO format)"),
    limit: int = Query(100, ge=1, le=1000, description="Max results"),
    offset: int = Query(0, ge=0, description="Results offset"),
    db: Session = Depends(get_db),
    current_admin: PlatformAdminResponse = Depends(get_current_platform_admin),
):
    """
    Search audit logs with filters.

    Returns:
        Paginated list of audit logs
    """
    try:
        audit_service = EnhancedAuditService(db)

        # Parse UUIDs if provided
        parsed_tenant_id = UUID(tenant_id) if tenant_id else None
        parsed_user_id = UUID(user_id) if user_id else None

        # Parse dates if provided
        parsed_start = datetime.fromisoformat(start_date) if start_date else None
        parsed_end = datetime.fromisoformat(end_date) if end_date else None

        results = audit_service.search_audit_logs(
            tenant_id=parsed_tenant_id,
            user_id=parsed_user_id,
            action=action,
            resource_type=resource_type,
            start_date=parsed_start,
            end_date=parsed_end,
            limit=limit,
            offset=offset,
        )

        return results

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid parameter: {str(e)}",
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to search audit logs: {str(e)}",
        )


@router.get("/logs/export", status_code=status.HTTP_200_OK, dependencies=[Depends(platform_admin_rate_limit)])
def export_audit_logs(
    format: str = Query("csv", regex="^(csv|json)$", description="Export format"),
    tenant_id: Optional[str] = Query(None, description="Tenant ID filter"),
    start_date: Optional[str] = Query(None, description="Start date (ISO format)"),
    end_date: Optional[str] = Query(None, description="End date (ISO format)"),
    db: Session = Depends(get_db),
    current_admin: PlatformAdminResponse = Depends(get_current_platform_admin),
):
    """
    Export audit logs to CSV or JSON.

    Returns:
        File download with audit logs
    """
    try:
        audit_service = EnhancedAuditService(db)

        # Parse parameters
        parsed_tenant_id = UUID(tenant_id) if tenant_id else None
        parsed_start = datetime.fromisoformat(start_date) if start_date else None
        parsed_end = datetime.fromisoformat(end_date) if end_date else None

        export_data = audit_service.export_audit_logs(
            format=format,
            tenant_id=parsed_tenant_id,
            start_date=parsed_start,
            end_date=parsed_end,
        )

        # Set appropriate content type and filename
        media_type = "text/csv" if format == "csv" else "application/json"
        filename = f"audit_logs_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.{format}"

        return Response(
            content=export_data,
            media_type=media_type,
            headers={
                "Content-Disposition": f"attachment; filename={filename}"
            },
        )

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid parameter: {str(e)}",
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to export audit logs: {str(e)}",
        )


@router.get("/tenant/{tenant_id}/activity", status_code=status.HTTP_200_OK, dependencies=[Depends(platform_admin_rate_limit)])
def get_tenant_activity(
    tenant_id: UUID,
    days: int = Query(30, ge=1, le=365, description="Days to analyze"),
    db: Session = Depends(get_db),
    current_admin: PlatformAdminResponse = Depends(get_current_platform_admin),
):
    """
    Get activity summary for a specific tenant.

    Returns:
        Tenant activity summary with breakdown by action type
    """
    try:
        audit_service = EnhancedAuditService(db)
        summary = audit_service.get_tenant_activity_summary(
            tenant_id=tenant_id,
            days=days,
        )

        return summary

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e),
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch tenant activity: {str(e)}",
        )


@router.get("/security-events", status_code=status.HTTP_200_OK, dependencies=[Depends(platform_admin_rate_limit)])
def get_security_events(
    severity: Optional[str] = Query(None, regex="^(high|medium|low)$", description="Severity filter"),
    days: int = Query(7, ge=1, le=90, description="Days to analyze"),
    limit: int = Query(100, ge=1, le=1000, description="Max results"),
    db: Session = Depends(get_db),
    current_admin: PlatformAdminResponse = Depends(get_current_platform_admin),
):
    """
    Get security-related events from audit logs.

    Returns:
        Security events with severity classification
    """
    try:
        audit_service = EnhancedAuditService(db)
        events = audit_service.get_security_events(
            severity=severity,
            days=days,
            limit=limit,
        )

        return events

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch security events: {str(e)}",
        )
