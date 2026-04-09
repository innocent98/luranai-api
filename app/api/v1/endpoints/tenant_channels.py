"""Tenant channel publishing endpoints."""

from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.core.config import settings
from app.db.models.tenant import Tenant
from app.schemas.customer_engagement import (
    TenantChannelOverviewResponse,
    TenantChannelPostCreate,
    TenantChannelPostResponse,
    TenantChannelPostUpdate,
)
from app.schemas.user import UserResponse
from app.services.customer_engagement_service import CustomerEngagementService

router = APIRouter()


@router.post("/upload-image", status_code=status.HTTP_200_OK)
async def upload_channel_image(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_user),
):
    """Upload an image for a channel post (cover image or inline content image)."""
    from app.utils.cloudinary_client import CloudinaryManager

    tenant = db.execute(
        select(Tenant).where(Tenant.id == current_user.tenant_id)
    ).scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    file_content = await file.read()
    manager = CloudinaryManager()
    url, error = manager.upload_channel_image(
        file_content=file_content,
        tenant_id=tenant.id,
        filename=file.filename or "channel_image",
    )
    if error:
        raise HTTPException(status_code=400, detail=error)

    return {"url": url}


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
async def create_tenant_channel_post(
    payload: TenantChannelPostCreate,
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_user),
):
    service = CustomerEngagementService(db)
    try:
        post = service.create_tenant_post(current_user, payload)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc

    # If published immediately, enqueue notification emails in background
    if payload.publish_now:
        try:
            from arq import create_pool
            from arq.connections import RedisSettings

            redis = await create_pool(RedisSettings.from_dsn(settings.REDIS_URL))
            await redis.enqueue_job("send_channel_post_notifications", str(post.id))
        except Exception as e:
            logger.error(f"Failed to enqueue channel notification job: {e}")

    return post


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
async def publish_tenant_channel_post(
    post_id: UUID,
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_user),
):
    service = CustomerEngagementService(db)
    try:
        post = service.publish_tenant_post(current_user, post_id)
    except ValueError as exc:
        message = str(exc)
        status_code = (
            status.HTTP_404_NOT_FOUND
            if "not found" in message.lower()
            else status.HTTP_403_FORBIDDEN
        )
        raise HTTPException(status_code=status_code, detail=message) from exc

    # Enqueue notification emails in background
    try:
        from arq import create_pool
        from arq.connections import RedisSettings

        redis = await create_pool(RedisSettings.from_dsn(settings.REDIS_URL))
        await redis.enqueue_job("send_channel_post_notifications", str(post.id))
    except Exception as e:
        logger.error(f"Failed to enqueue channel notification job: {e}")

    return post


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
