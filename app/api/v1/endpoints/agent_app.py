"""Agent Caller mobile-app API (`/api/v1/agent`).

Every endpoint requires an agent JWT and is scoped to `current_agent.tenant_id`.
A resource belonging to another tenant returns 404, never 403 — matching the
auditor's `_owned_report` pattern, so the API never confirms existence
cross-tenant.
"""

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_agent
from app.db.models.agent_device import AgentDevice
from app.db.models.tenant import Tenant
from app.db.models.user import User
from app.db.session import get_db
from app.schemas.agent_app import (
    AgentMeResponse,
    AvailabilityResponse,
    AvailabilityUpdateRequest,
    DeviceRegisterRequest,
    DeviceUnregisterRequest,
)
from app.schemas.user import UserResponse
from app.utils.datetime import utcnow_naive

router = APIRouter()


@router.get("/me", response_model=AgentMeResponse)
def get_agent_me(
    db: Session = Depends(get_db),
    current_agent: UserResponse = Depends(get_current_agent),
):
    """Identity, tenant display name and availability in one call (addendum A1)."""
    tenant = db.get(Tenant, current_agent.tenant_id)
    user = db.get(User, current_agent.id)
    return AgentMeResponse(
        user_id=current_agent.id,
        email=current_agent.email,
        full_name=current_agent.full_name,
        role=current_agent.role,
        tenant_id=current_agent.tenant_id,
        tenant_name=(tenant.display_name if tenant else "") or "",
        availability_status=user.availability_status,
    )


@router.post("/devices", status_code=status.HTTP_204_NO_CONTENT)
def register_device(
    payload: DeviceRegisterRequest,
    db: Session = Depends(get_db),
    current_agent: UserResponse = Depends(get_current_agent),
):
    """Upsert this device's FCM token. Called after login and on every cold start."""
    device = db.execute(
        select(AgentDevice).where(
            AgentDevice.user_id == current_agent.id,
            AgentDevice.device_token == payload.device_token,
        )
    ).scalar_one_or_none()

    if device is None:
        device = AgentDevice(
            user_id=current_agent.id,
            tenant_id=current_agent.tenant_id,
            device_token=payload.device_token,
            platform=payload.platform,
        )
        db.add(device)

    device.platform = payload.platform
    device.last_seen_at = utcnow_naive()
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/devices", status_code=status.HTTP_204_NO_CONTENT)
def unregister_device(
    payload: DeviceUnregisterRequest,
    db: Session = Depends(get_db),
    current_agent: UserResponse = Depends(get_current_agent),
):
    """Remove this device on logout. Unknown tokens are a silent 204."""
    device = db.execute(
        select(AgentDevice).where(
            AgentDevice.user_id == current_agent.id,
            AgentDevice.device_token == payload.device_token,
        )
    ).scalar_one_or_none()
    if device is not None:
        db.delete(device)
        db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/availability", response_model=AvailabilityResponse)
def set_availability(
    payload: AvailabilityUpdateRequest,
    db: Session = Depends(get_db),
    current_agent: UserResponse = Depends(get_current_agent),
):
    """Manual Available/Away toggle. Only `available` agents ring in tier 1."""
    user = db.get(User, current_agent.id)
    user.availability_status = payload.status
    db.commit()
    return AvailabilityResponse(status=payload.status)
