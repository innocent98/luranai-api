"""Tenant channel publishing endpoints."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.schemas.customer_engagement import (
    TenantChannelOverviewResponse,
    TenantChannelPostCreate,
    TenantChannelPostResponse,
    TenantChannelPostUpdate,
)
from app.schemas.user import UserResponse
from app.services.customer_engagement_service import CustomerEngagementService

router = APIRouter()


@router.get(
    "",
    response_model=list[TenantChannelPostResponse],
    status_code=status.HTTP_200_OK,
)
def list_tenant_channels(
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_user),
):
    service = CustomerEngagementService(db)
    try:
        return service.list_tenant_posts(current_user)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc


@router.get(
    "/overview",
    response_model=TenantChannelOverviewResponse,
    status_code=status.HTTP_200_OK,
)
def get_tenant_channels_overview(
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_user),
):
    service = CustomerEngagementService(db)
    try:
        return TenantChannelOverviewResponse(**service.tenant_overview(current_user))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc


@router.post(
    "",
    response_model=TenantChannelPostResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_tenant_channel_post(
    payload: TenantChannelPostCreate,
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_user),
):
    service = CustomerEngagementService(db)
    try:
        return service.create_tenant_post(current_user, payload)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc


@router.put(
    "/{post_id}",
    response_model=TenantChannelPostResponse,
    status_code=status.HTTP_200_OK,
)
def update_tenant_channel_post(
    post_id: UUID,
    payload: TenantChannelPostUpdate,
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_user),
):
    service = CustomerEngagementService(db)
    try:
        return service.update_tenant_post(current_user, post_id, payload)
    except ValueError as exc:
        message = str(exc)
        status_code = (
            status.HTTP_404_NOT_FOUND
            if "not found" in message.lower()
            else status.HTTP_403_FORBIDDEN
        )
        raise HTTPException(status_code=status_code, detail=message) from exc


@router.post(
    "/{post_id}/publish",
    response_model=TenantChannelPostResponse,
    status_code=status.HTTP_200_OK,
)
def publish_tenant_channel_post(
    post_id: UUID,
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_user),
):
    service = CustomerEngagementService(db)
    try:
        return service.publish_tenant_post(current_user, post_id)
    except ValueError as exc:
        message = str(exc)
        status_code = (
            status.HTTP_404_NOT_FOUND
            if "not found" in message.lower()
            else status.HTTP_403_FORBIDDEN
        )
        raise HTTPException(status_code=status_code, detail=message) from exc


@router.post(
    "/{post_id}/archive",
    response_model=TenantChannelPostResponse,
    status_code=status.HTTP_200_OK,
)
def archive_tenant_channel_post(
    post_id: UUID,
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_user),
):
    service = CustomerEngagementService(db)
    try:
        return service.archive_tenant_post(current_user, post_id)
    except ValueError as exc:
        message = str(exc)
        status_code = (
            status.HTTP_404_NOT_FOUND
            if "not found" in message.lower()
            else status.HTTP_403_FORBIDDEN
        )
        raise HTTPException(status_code=status_code, detail=message) from exc
