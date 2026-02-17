"""Platform audit log management API endpoints."""

import os
import re
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_platform_admin, get_db
from app.core.config import settings
from app.db.models.platform_admin import PlatformAdminRole
from app.schemas.platform_admin import PlatformAdminResponse
from app.services.enhanced_audit_service import EnhancedAuditService
from app.utils.http_rate_limiter import platform_admin_rate_limit

router = APIRouter()
LOG_LINE_RE = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}) \| "
    r"(?P<level>[A-Z]+)\s+\| (?P<source>[^-]+) - (?P<message>.*)$"
)


def _require_platform_log_access(current_admin: PlatformAdminResponse) -> None:
    if current_admin.role not in {PlatformAdminRole.SUPER_ADMIN, PlatformAdminRole.ADMIN}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not enough permissions. Admin access required.",
        )


def _read_last_lines(file_path: Path, lines: int) -> list[str]:
    with file_path.open("r", encoding="utf-8", errors="replace") as handle:
        return list(deque(handle, maxlen=lines))


def _resolve_log_candidates(include_rotated: bool) -> list[Path]:
    log_path = Path(settings.LOG_FILE_PATH)
    if not log_path.is_absolute():
        log_path = Path("/app") / log_path
    log_dir = log_path.parent

    if not log_dir.exists():
        return []

    base_name = log_path.name
    candidates = [log_dir / base_name]
    if include_rotated:
        # Include rotated files like app.2026-...log.zip (or similar)
        rotated = sorted(log_dir.glob(f"{base_name}*"), reverse=True)
        seen = {str(c) for c in candidates}
        for item in rotated:
            if str(item) not in seen and item.is_file():
                candidates.append(item)
                seen.add(str(item))

    return [c for c in candidates if c.exists() and c.is_file() and c.suffix != ".zip"]


@router.get(
    "/logs", status_code=status.HTTP_200_OK, dependencies=[Depends(platform_admin_rate_limit)]
)
def search_audit_logs(
    tenant_id: str | None = Query(None, description="Tenant ID filter"),
    user_id: str | None = Query(None, description="User ID filter"),
    action: str | None = Query(None, description="Action filter (partial match)"),
    resource_type: str | None = Query(None, description="Resource type filter (partial match)"),
    start_date: str | None = Query(None, description="Start date (ISO format)"),
    end_date: str | None = Query(None, description="End date (ISO format)"),
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
        ) from e
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to search audit logs: {str(e)}",
        ) from e


@router.get(
    "/logs/export",
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(platform_admin_rate_limit)],
)
def export_audit_logs(
    format: str = Query("csv", pattern="^(csv|json)$", description="Export format"),
    tenant_id: str | None = Query(None, description="Tenant ID filter"),
    start_date: str | None = Query(None, description="Start date (ISO format)"),
    end_date: str | None = Query(None, description="End date (ISO format)"),
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
        filename = f"audit_logs_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}.{format}"

        return Response(
            content=export_data,
            media_type=media_type,
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid parameter: {str(e)}",
        ) from e
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to export audit logs: {str(e)}",
        ) from e


@router.get(
    "/tenant/{tenant_id}/activity",
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(platform_admin_rate_limit)],
)
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
        ) from e
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch tenant activity: {str(e)}",
        ) from e


@router.get(
    "/security-events",
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(platform_admin_rate_limit)],
)
def get_security_events(
    severity: str | None = Query(
        None, pattern="^(high|medium|low)$", description="Severity filter"
    ),
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
        ) from e


@router.get(
    "/runtime-logs",
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(platform_admin_rate_limit)],
)
def get_runtime_logs(
    lines: int = Query(200, ge=20, le=1000, description="Max log lines to return"),
    level: str | None = Query(
        None,
        pattern="^(TRACE|DEBUG|INFO|SUCCESS|WARNING|ERROR|CRITICAL)$",
        description="Optional log level filter",
    ),
    query: str | None = Query(None, max_length=200, description="Text search filter"),
    include_rotated: bool = Query(
        False,
        description="Include rotated log files (excluding compressed archives)",
    ),
    current_admin: PlatformAdminResponse = Depends(get_current_platform_admin),
):
    """
    Read runtime application logs from local rotating log files.

    Access:
        SUPER_ADMIN and ADMIN only.
    """
    try:
        _require_platform_log_access(current_admin)

        candidates = _resolve_log_candidates(include_rotated=include_rotated)
        if not candidates:
            return {
                "lines": [],
                "meta": {
                    "total": 0,
                    "returned": 0,
                    "files": [],
                    "lines_requested": lines,
                },
            }

        aggregated: list[str] = []
        for file_path in candidates:
            aggregated.extend(_read_last_lines(file_path, lines))
            if len(aggregated) >= lines * 4:
                # Guard rail to keep response bounded when include_rotated=true
                aggregated = aggregated[-(lines * 4) :]

        parsed_lines = []
        normalized_level = level.upper() if level else None
        normalized_query = query.lower() if query else None

        for raw in aggregated:
            text = raw.rstrip("\n")
            if not text.strip():
                continue

            match = LOG_LINE_RE.match(text)
            if match:
                item = {
                    "raw": text,
                    "timestamp": match.group("timestamp"),
                    "level": match.group("level"),
                    "source": match.group("source").strip(),
                    "message": match.group("message"),
                }
            else:
                item = {
                    "raw": text,
                    "timestamp": None,
                    "level": None,
                    "source": None,
                    "message": text,
                }

            if normalized_level and item.get("level") != normalized_level:
                continue
            if normalized_query and normalized_query not in text.lower():
                continue

            parsed_lines.append(item)

        # Return latest N after filtering
        parsed_lines = parsed_lines[-lines:]

        return {
            "lines": parsed_lines,
            "meta": {
                "total": len(parsed_lines),
                "returned": len(parsed_lines),
                "files": [os.path.basename(str(p)) for p in candidates],
                "lines_requested": lines,
            },
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch runtime logs: {str(e)}",
        ) from e
