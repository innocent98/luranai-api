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

router = APIRouter()


def _err(code: str, message: str, http: int, details: dict | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=http,
        content={
            "success": False,
            "error": {"code": code, "message": message, "details": details or {}},
        },
    )


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

    if row is None:
        row = TenantSalesAgentConfig.default_for(user.tenant_id)
        db.add(row)

    # Apply only the fields explicitly provided (non-None)
    for field, value in payload.model_dump(exclude_none=True).items():
        setattr(row, field, value)

    db.commit()
    db.refresh(row)

    config = SalesConfig.model_validate(row)
    return {"success": True, "data": config.model_dump()}
