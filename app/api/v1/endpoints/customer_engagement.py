"""Customer follow and channels endpoints."""

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_customer, get_db
from app.schemas.customer_auth import CustomerUserResponse
from app.schemas.customer_engagement import (
    CustomerChannelPostDetail,
    CustomerChannelPostSummary,
    CustomerFollowResponse,
    CustomerFollowStatusResponse,
)
from app.services.customer_engagement_service import CustomerEngagementService

router = APIRouter()


@router.get("/follows", response_model=list[CustomerFollowResponse], status_code=status.HTTP_200_OK)
def list_customer_follows(
    db: Session = Depends(get_db),
    current_customer: CustomerUserResponse = Depends(get_current_customer),
):
    service = CustomerEngagementService(db)
    rows = service.list_follows(current_customer.id)
    return [
        CustomerFollowResponse(
            tenant_id=tenant.id,
            business_name=tenant.display_name,
            business_slug=tenant.public_slug,
            business_logo_url=tenant.widget_logo_url,
            business_description=tenant.description,
            notifications_enabled=follow.notifications_enabled,
            followed_at=follow.created_at,
            latest_post_title=post.title if post else None,
            latest_post_published_at=post.published_at if post else None,
        )
        for follow, tenant, post in rows
    ]


@router.get(
    "/follows/{tenant_id}/status",
    response_model=CustomerFollowStatusResponse,
    status_code=status.HTTP_200_OK,
)
def get_follow_status(
    tenant_id: UUID,
    db: Session = Depends(get_db),
    current_customer: CustomerUserResponse = Depends(get_current_customer),
):
    service = CustomerEngagementService(db)
    tenant = service.get_tenant(tenant_id)
    if not tenant:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Business not found")

    follow = service.get_follow(current_customer.id, tenant_id)
    return CustomerFollowStatusResponse(
        tenant_id=tenant_id,
        is_following=follow is not None,
        follower_count=service.follower_count(tenant_id),
        published_post_count=service.published_post_count(tenant_id),
    )


@router.post(
    "/follows/{tenant_id}",
    response_model=CustomerFollowStatusResponse,
    status_code=status.HTTP_201_CREATED,
)
def follow_business(
    tenant_id: UUID,
    db: Session = Depends(get_db),
    current_customer: CustomerUserResponse = Depends(get_current_customer),
):
    service = CustomerEngagementService(db)
    try:
        service.follow_business(current_customer.id, tenant_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    return CustomerFollowStatusResponse(
        tenant_id=tenant_id,
        is_following=True,
        follower_count=service.follower_count(tenant_id),
        published_post_count=service.published_post_count(tenant_id),
    )


@router.delete(
    "/follows/{tenant_id}",
    response_model=CustomerFollowStatusResponse,
    status_code=status.HTTP_200_OK,
)
def unfollow_business(
    tenant_id: UUID,
    db: Session = Depends(get_db),
    current_customer: CustomerUserResponse = Depends(get_current_customer),
):
    service = CustomerEngagementService(db)
    service.unfollow_business(current_customer.id, tenant_id)
    return CustomerFollowStatusResponse(
        tenant_id=tenant_id,
        is_following=False,
        follower_count=service.follower_count(tenant_id),
        published_post_count=service.published_post_count(tenant_id),
    )


@router.get("/channels", status_code=status.HTTP_200_OK)
def list_customer_channels(
    following_only: bool = Query(True),
    tenant_id: UUID | None = Query(None),
    limit: int = Query(30, ge=1, le=100),
    before: datetime | None = Query(None, description="ISO timestamp cursor for pagination"),
    search: str | None = Query(None, description="Search by business name"),
    db: Session = Depends(get_db),
    current_customer: CustomerUserResponse = Depends(get_current_customer),
):
    service = CustomerEngagementService(db)
    rows, has_more = service.list_customer_feed(
        customer_user_id=current_customer.id,
        following_only=following_only,
        tenant_id=tenant_id,
        limit=limit,
        before=before,
        search=search,
    )
    return {
        "posts": [
            CustomerChannelPostSummary(
                id=post.id,
                tenant_id=post.tenant_id,
                business_name=tenant.display_name,
                business_slug=tenant.public_slug,
                business_logo_url=tenant.widget_logo_url,
                title=post.title,
                excerpt=post.excerpt,
                cover_image_url=post.cover_image_url,
                cta_label=post.cta_label,
                cta_url=post.cta_url,
                is_pinned=post.is_pinned,
                is_following=is_following,
                published_at=post.published_at,
            )
            for post, tenant, is_following in rows
        ],
        "has_more": has_more,
    }


@router.get(
    "/channels/{post_id}",
    response_model=CustomerChannelPostDetail,
    status_code=status.HTTP_200_OK,
)
def get_customer_channel_detail(
    post_id: UUID,
    db: Session = Depends(get_db),
    current_customer: CustomerUserResponse = Depends(get_current_customer),
):
    service = CustomerEngagementService(db)
    row = service.get_customer_post(current_customer.id, post_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Channel post not found")

    post, tenant, is_following = row
    return CustomerChannelPostDetail(
        id=post.id,
        tenant_id=post.tenant_id,
        business_name=tenant.display_name,
        business_slug=tenant.public_slug,
        business_logo_url=tenant.widget_logo_url,
        title=post.title,
        excerpt=post.excerpt,
        content=post.content,
        cover_image_url=post.cover_image_url,
        cta_label=post.cta_label,
        cta_url=post.cta_url,
        is_pinned=post.is_pinned,
        is_following=is_following,
        published_at=post.published_at,
    )
