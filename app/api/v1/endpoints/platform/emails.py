"""Platform admin email campaign endpoints."""

from uuid import UUID

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from sqlalchemy.orm import Session

from app.api.deps import get_current_platform_admin, get_db
from app.db.session import SessionLocal
from app.schemas.platform_admin import PlatformAdminResponse
from app.schemas.platform_email_campaign import (
    PlatformEmailCampaignCreateRequest,
    PlatformEmailCampaignDetailResponse,
    PlatformEmailImageUploadResponse,
    PlatformEmailCampaignSummaryResponse,
    PlatformEmailCampaignTestSendRequest,
    PlatformEmailCampaignTestSendResponse,
)
from app.services.platform_email_campaign_service import PlatformEmailCampaignService
from app.utils.http_rate_limiter import platform_admin_rate_limit

router = APIRouter()


def _send_campaign_in_background(campaign_id: UUID) -> None:
    db = SessionLocal()
    try:
        service = PlatformEmailCampaignService(db)
        service.send_campaign(campaign_id)
    finally:
        db.close()


@router.post(
    "/upload-image",
    response_model=PlatformEmailImageUploadResponse,
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(platform_admin_rate_limit)],
)
async def upload_campaign_image(
    file: UploadFile = File(..., description="Campaign image file (JPEG, PNG, GIF, WebP)"),
    current_admin: PlatformAdminResponse = Depends(get_current_platform_admin),
):
    """Upload an image for use inside platform email campaigns."""
    from app.utils.cloudinary_client import cloudinary_manager

    try:
        file_content = await file.read()
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Failed to read the selected image.",
        ) from e

    image_url, error = cloudinary_manager.upload_platform_email_image(
        file_content=file_content,
        admin_id=current_admin.id,
        filename=file.filename or "campaign-image",
    )

    if error:
        status_code = (
            status.HTTP_503_SERVICE_UNAVAILABLE
            if "not configured" in error.lower()
            else status.HTTP_400_BAD_REQUEST
        )
        raise HTTPException(status_code=status_code, detail=error)

    return PlatformEmailImageUploadResponse(
        message="Image uploaded successfully",
        image_url=image_url or "",
    )


@router.post(
    "/campaigns",
    response_model=PlatformEmailCampaignSummaryResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(platform_admin_rate_limit)],
)
def create_campaign(
    request: PlatformEmailCampaignCreateRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_admin: PlatformAdminResponse = Depends(get_current_platform_admin),
):
    """Create and queue a broadcast email campaign."""
    service = PlatformEmailCampaignService(db)

    try:
        campaign = service.create_campaign(request, admin_id=current_admin.id)
        if not request.scheduled_at:
            background_tasks.add_task(_send_campaign_in_background, campaign.id)
        return campaign
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create campaign right now. Please try again shortly.",
        ) from e


@router.post(
    "/test-send",
    response_model=PlatformEmailCampaignTestSendResponse,
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(platform_admin_rate_limit)],
)
def send_test_email(
    request: PlatformEmailCampaignTestSendRequest,
    db: Session = Depends(get_db),
    current_admin: PlatformAdminResponse = Depends(get_current_platform_admin),
):
    """Send a test version of a campaign to the current platform admin."""
    service = PlatformEmailCampaignService(db)
    try:
        success = service.send_test_email(
            request,
            recipient_email=current_admin.email,
            recipient_name=current_admin.full_name,
        )
        if not success:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Test email could not be sent. Check SMTP configuration and try again.",
            )
        return PlatformEmailCampaignTestSendResponse(
            success=True,
            message=f"Test email sent to {current_admin.email}",
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unable to send a test email right now. Please try again shortly.",
        ) from e


@router.get(
    "/campaigns",
    response_model=list[PlatformEmailCampaignSummaryResponse],
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(platform_admin_rate_limit)],
)
def list_campaigns(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_admin: PlatformAdminResponse = Depends(get_current_platform_admin),
):
    """List recent email campaigns."""
    service = PlatformEmailCampaignService(db)
    return service.list_campaigns(limit=limit, offset=offset)


@router.get(
    "/campaigns/{campaign_id}",
    response_model=PlatformEmailCampaignDetailResponse,
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(platform_admin_rate_limit)],
)
def get_campaign(
    campaign_id: UUID,
    db: Session = Depends(get_db),
    current_admin: PlatformAdminResponse = Depends(get_current_platform_admin),
):
    """Get detailed campaign information and recipient statuses."""
    service = PlatformEmailCampaignService(db)

    try:
        return service.get_campaign_detail(campaign_id)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to load campaign details right now.",
        ) from e
