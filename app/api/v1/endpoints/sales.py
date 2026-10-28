"""Authenticated tenant-admin endpoints for Sales Agent configuration."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_tenant_admin, get_db
from app.db.models.sales_agent_config import TenantSalesAgentConfig
from app.schemas.sales_config import SalesConfig, SalesConfigPatch
from app.schemas.user import UserResponse
from app.services.entitlements import require_feature

router = APIRouter()


def _err(code: str, message: str, http: int, details: dict | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=http,
        content={
            "success": False,
            "error": {"code": code, "message": message, "details": details or {}},
        },
    )


def _require_entitlements_for(
    db: Session, tenant_id, row: TenantSalesAgentConfig, changes: dict
) -> None:
    """Reject a save that switches on a gated option the plan does not include.

    "Enabling" means going from off/empty to on/non-empty, so a tenant that keeps an option it
    already had (and is later downgraded) can still save its other settings. Runtime paths skip
    the gated behaviour on their own.
    """
    saved_channels = row.notify_channels or {}
    new_channels = changes.get("notify_channels") or {}
    enabling_sales = (
        changes.get("meeting_booking_enabled") and not row.meeting_booking_enabled
    ) or (changes.get("quotation_enabled") and not row.quotation_enabled)
    enabling_crm = (
        any(new_channels.get(c) and not saved_channels.get(c) for c in ("slack", "crm"))
        or bool(changes.get("slack_webhook_url"))
        and changes.get("slack_webhook_url") != row.slack_webhook_url
        or bool(changes.get("crm_webhook_url"))
        and changes.get("crm_webhook_url") != row.crm_webhook_url
    )
    if enabling_sales:
        require_feature(db, tenant_id, "sales_close")
    if enabling_crm:
        require_feature(db, tenant_id, "crm_notifications")


@router.get("/config")
def get_sales_config(
    db: Session = Depends(get_db),
    user: UserResponse = Depends(get_current_tenant_admin),
):
    """Return this tenant's sales agent config.

    If no row exists, returns the default config serialized — does NOT persist it.
    """
    row = db.execute(
        select(TenantSalesAgentConfig).where(TenantSalesAgentConfig.tenant_id == user.tenant_id)
    ).scalar_one_or_none()

    if row is None:
        row = TenantSalesAgentConfig.default_for(user.tenant_id)

    config = SalesConfig.model_validate(row)
    return {"success": True, "data": config.model_dump()}


@router.patch("/config")
def patch_sales_config(
    payload: SalesConfigPatch,
    db: Session = Depends(get_db),
    user: UserResponse = Depends(get_current_tenant_admin),
):
    """Upsert this tenant's sales agent config.

    If no row exists, create one from defaults then apply provided fields.
    Only non-None fields in the patch body are applied.
    """
    row = db.execute(
        select(TenantSalesAgentConfig).where(TenantSalesAgentConfig.tenant_id == user.tenant_id)
    ).scalar_one_or_none()

    is_new = row is None
    if row is None:
        row = TenantSalesAgentConfig.default_for(user.tenant_id)

    changes = payload.model_dump(exclude_none=True)
    _require_entitlements_for(db, user.tenant_id, row, changes)
    if is_new:
        db.add(row)

    # Apply only the fields explicitly provided (non-None)
    for field, value in changes.items():
        setattr(row, field, value)

    db.commit()
    db.refresh(row)

    config = SalesConfig.model_validate(row)
    return {"success": True, "data": config.model_dump()}
