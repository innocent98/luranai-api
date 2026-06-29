"""Authenticated tenant-admin endpoints for in-app notifications (Stage 3 Slice 5c)."""

from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.api.deps import get_current_tenant_admin, get_db
from app.db.models.notification_log import NotifChannel, NotificationLog
from app.schemas.notification import NotificationItem, NotificationList
from app.schemas.user import UserResponse

router = APIRouter()


def _err(code: str, message: str, http: int) -> JSONResponse:
    return JSONResponse(
        status_code=http,
        content={"success": False, "error": {"code": code, "message": message}},
    )


def _base_stmt(tenant_id):
    """Base SELECT filtered to this tenant's in-app notifications."""
    return select(NotificationLog).where(
        NotificationLog.tenant_id == tenant_id,
        NotificationLog.channel == NotifChannel.IN_APP,
    )


@router.get("")
def list_notifications(
    unread_only: bool = Query(default=False),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    user: UserResponse = Depends(get_current_tenant_admin),
):
    """List this tenant's in-app notifications, newest-first."""
    stmt = _base_stmt(user.tenant_id)
    if unread_only:
        stmt = stmt.where(NotificationLog.read_at.is_(None))

    total = db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    rows = (
        db.execute(
            stmt.order_by(NotificationLog.created_at.desc()).limit(limit).offset(offset)
        )
        .scalars()
        .all()
    )

    items = [
        NotificationItem(
            id=r.id,
            event=r.event.value,
            title=r.title,
            body=r.body,
            read_at=r.read_at,
            created_at=r.created_at,
        )
        for r in rows
    ]
    payload = NotificationList(items=items, total=total, limit=limit, offset=offset)
    return {"success": True, "data": payload.model_dump()}


@router.get("/unread-count")
def unread_count(
    db: Session = Depends(get_db),
    user: UserResponse = Depends(get_current_tenant_admin),
):
    """Return count of unread in-app notifications for this tenant."""
    stmt = _base_stmt(user.tenant_id).where(NotificationLog.read_at.is_(None))
    count = db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    return {"success": True, "data": {"count": count}}


# NOTE: /read-all is registered BEFORE /{notification_id}/read so the literal
# route wins over the path-param route (FastAPI matches in declaration order).
@router.patch("/read-all")
def mark_all_read(
    db: Session = Depends(get_db),
    user: UserResponse = Depends(get_current_tenant_admin),
):
    """Mark all unread in-app notifications as read for this tenant."""
    res = db.execute(
        update(NotificationLog)
        .where(
            NotificationLog.tenant_id == user.tenant_id,
            NotificationLog.channel == NotifChannel.IN_APP,
            NotificationLog.read_at.is_(None),
        )
        .values(read_at=datetime.now(timezone.utc))
    )
    db.commit()
    return {"success": True, "data": {"updated": res.rowcount or 0}}


@router.patch("/{notification_id}/read")
def mark_read(
    notification_id: UUID,
    db: Session = Depends(get_db),
    user: UserResponse = Depends(get_current_tenant_admin),
):
    """Mark a single in-app notification as read (404 if not found or wrong tenant)."""
    row = (
        db.execute(
            _base_stmt(user.tenant_id).where(NotificationLog.id == notification_id)
        )
        .scalars()
        .first()
    )
    if row is None:
        return _err("NOTIFICATION_NOT_FOUND", "Not found", 404)
    if row.read_at is None:
        row.read_at = datetime.now(timezone.utc)
        db.commit()
    return {
        "success": True,
        "data": {
            "id": str(row.id),
            "read_at": row.read_at.isoformat() if row.read_at else None,
        },
    }
