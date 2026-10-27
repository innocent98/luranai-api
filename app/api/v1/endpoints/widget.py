"""Widget configuration and public endpoints."""

from fastapi import (
    APIRouter,
    Depends,
    File,
    Header,
    HTTPException,
    Path,
    Request,
    UploadFile,
    status,
)
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_tenant_admin, get_db
from app.db.models.tenant import Tenant, TenantCapabilities
from app.schemas.user import UserResponse
from app.schemas.widget import (
    PublicWidgetConfigResponse,
    WidgetConfigResponse,
    WidgetConfigUpdate,
    WidgetInstallCodeResponse,
)
from app.services.api_key_service import APIKeyService
from app.services.screen_recording.gating import screen_recording_allowed
from app.services.widget_seen_service import record_widget_seen
from app.services.visit_analytics_service import VisitAnalyticsService

router = APIRouter()


def _widget_config_response(db: Session, tenant: Tenant) -> WidgetConfigResponse:
    caps = db.execute(
        select(TenantCapabilities).where(TenantCapabilities.tenant_id == tenant.id)
    ).scalar_one_or_none()
    return WidgetConfigResponse(
        tenant_id=tenant.id,
        company_name=tenant.display_name,
        widget_primary_color=tenant.widget_primary_color,
        widget_accent_color=tenant.widget_accent_color,
        widget_logo_url=tenant.widget_logo_url,
        widget_greeting_message=tenant.widget_greeting_message,
        widget_position=tenant.widget_position,
        widget_show_unread_badge=tenant.widget_show_unread_badge,
        widget_allow_file_upload=tenant.widget_allow_file_upload,
        widget_allow_screen_recording=tenant.widget_allow_screen_recording,
        can_use_screen_recording=bool(caps and caps.can_use_screen_recording),
    )


@router.get(
    "/config",
    response_model=WidgetConfigResponse,
    status_code=status.HTTP_200_OK,
)
def get_widget_config(
    current_user: UserResponse = Depends(get_current_tenant_admin),
    db: Session = Depends(get_db),
):
    """
    Get widget configuration for the tenant.

    Args:
        current_user: Current authenticated tenant admin
        db: Database session

    Returns:
        Widget configuration

    Raises:
        HTTPException: 403 if not tenant admin
        HTTPException: 404 if tenant not found
    """
    stmt = select(Tenant).where(Tenant.id == current_user.tenant_id)
    tenant = db.execute(stmt).scalar_one_or_none()

    if not tenant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Tenant not found",
        )

    return _widget_config_response(db, tenant)


@router.patch(
    "/config",
    response_model=WidgetConfigResponse,
    status_code=status.HTTP_200_OK,
)
def update_widget_config(
    config_data: WidgetConfigUpdate,
    current_user: UserResponse = Depends(get_current_tenant_admin),
    db: Session = Depends(get_db),
):
    """
    Update widget configuration.

    Args:
        config_data: Widget configuration update data
        current_user: Current authenticated tenant admin
        db: Database session

    Returns:
        Updated widget configuration

    Raises:
        HTTPException: 403 if not tenant admin
        HTTPException: 404 if tenant not found
    """
    stmt = select(Tenant).where(Tenant.id == current_user.tenant_id)
    tenant = db.execute(stmt).scalar_one_or_none()

    if not tenant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Tenant not found",
        )

    # Update fields
    if config_data.widget_primary_color is not None:
        tenant.widget_primary_color = config_data.widget_primary_color

    if config_data.widget_accent_color is not None:
        tenant.widget_accent_color = config_data.widget_accent_color

    if config_data.widget_logo_url is not None:
        tenant.widget_logo_url = config_data.widget_logo_url

    if config_data.widget_greeting_message is not None:
        tenant.widget_greeting_message = config_data.widget_greeting_message

    if config_data.widget_position is not None:
        tenant.widget_position = config_data.widget_position

    if config_data.widget_show_unread_badge is not None:
        tenant.widget_show_unread_badge = config_data.widget_show_unread_badge

    if config_data.widget_allow_file_upload is not None:
        tenant.widget_allow_file_upload = config_data.widget_allow_file_upload

    if config_data.widget_allow_screen_recording is not None:
        tenant.widget_allow_screen_recording = config_data.widget_allow_screen_recording

    db.commit()
    db.refresh(tenant)

    return _widget_config_response(db, tenant)


@router.post(
    "/upload-logo",
    response_model=dict,
    status_code=status.HTTP_200_OK,
)
async def upload_widget_logo(
    file: UploadFile = File(..., description="Logo image file (JPEG, PNG, GIF, WebP)"),
    current_user: UserResponse = Depends(get_current_tenant_admin),
    db: Session = Depends(get_db),
):
    """
    Upload widget logo image to Cloudinary.

    The uploaded image will be:
    - Validated for format and size
    - Optimized and resized (max 500x500px)
    - Uploaded to Cloudinary
    - URL automatically saved to widget configuration

    Args:
        file: Image file to upload (max 5MB)
        current_user: Current authenticated tenant admin
        db: Database session

    Returns:
        dict with logo_url and success message

    Raises:
        HTTPException: 400 if file is invalid or upload fails
        HTTPException: 404 if tenant not found
        HTTPException: 503 if Cloudinary not configured
    """
    from app.utils.cloudinary_client import cloudinary_manager

    # Get tenant
    stmt = select(Tenant).where(Tenant.id == current_user.tenant_id)
    tenant = db.execute(stmt).scalar_one_or_none()

    if not tenant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Tenant not found",
        )

    # Read file content
    try:
        file_content = await file.read()
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to read file: {str(e)}",
        ) from e

    # Upload to Cloudinary
    logo_url, error = cloudinary_manager.upload_widget_logo(
        file_content=file_content,
        tenant_id=tenant.id,
        filename=file.filename or "logo",
    )

    if error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error,
        )

    # Update tenant widget config
    tenant.widget_logo_url = logo_url
    db.commit()
    db.refresh(tenant)

    return {
        "message": "Logo uploaded successfully",
        "logo_url": logo_url,
    }


@router.delete(
    "/logo",
    response_model=dict,
    status_code=status.HTTP_200_OK,
)
def delete_widget_logo(
    current_user: UserResponse = Depends(get_current_tenant_admin),
    db: Session = Depends(get_db),
):
    """
    Delete widget logo from Cloudinary and remove URL from config.

    Args:
        current_user: Current authenticated tenant admin
        db: Database session

    Returns:
        Success message

    Raises:
        HTTPException: 404 if tenant not found or no logo exists
    """
    from app.utils.cloudinary_client import cloudinary_manager

    # Get tenant
    stmt = select(Tenant).where(Tenant.id == current_user.tenant_id)
    tenant = db.execute(stmt).scalar_one_or_none()

    if not tenant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Tenant not found",
        )

    if not tenant.widget_logo_url:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No logo configured",
        )

    # Delete from Cloudinary
    success, error = cloudinary_manager.delete_widget_logo(tenant.id)

    # Remove URL from config regardless (in case logo was deleted manually from Cloudinary)
    tenant.widget_logo_url = None
    db.commit()

    return {
        "message": (
            "Logo deleted successfully"
            if success
            else "Logo URL removed (may have been deleted already)"
        ),
    }


@router.get(
    "/install-code",
    response_model=WidgetInstallCodeResponse,
    status_code=status.HTTP_200_OK,
)
def get_install_code(
    current_user: UserResponse = Depends(get_current_tenant_admin),
    db: Session = Depends(get_db),
):
    """
    Get widget installation code snippet.

    Args:
        current_user: Current authenticated tenant admin
        db: Database session

    Returns:
        Installation code and instructions

    Raises:
        HTTPException: 403 if not tenant admin
    """
    # Get tenant info
    stmt = select(Tenant).where(Tenant.id == current_user.tenant_id)
    tenant = db.execute(stmt).scalar_one_or_none()

    if not tenant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Tenant not found",
        )

    # Generate installation code
    # The loader starts on Luran.init(); it is loaded without async/defer so
    # `Luran` exists by the time the second tag runs.
    html_snippet = f"""<!-- Luran Widget Integration -->
<script src="https://cdn.luranai.com/widget/v1/widget.js"></script>
<script>
  Luran.init({{
    tenantId: '{tenant.id}',
    apiKey: 'YOUR_PUBLIC_API_KEY_HERE'  // Get from API Keys page
  }});
</script>
<!-- End Luran Widget -->"""

    instructions = """Installation Instructions:

1. Get your Public API Key:
   - Go to Settings → API Keys
   - Create a new PUBLIC API key
   - Copy the key (starts with pk_live_...)

2. Replace 'YOUR_PUBLIC_API_KEY_HERE' in the code above with your actual public API key

3. Add the code snippet to your website:
   - Paste it before the closing </body> tag
   - The widget will appear on all pages where this code is present

4. Customize the widget:
   - Go to Settings → Widget Configuration
   - Adjust colors, logo, greeting message, and position

5. Test the widget:
   - Visit your website
   - Click the floating chat button
   - Start a conversation

Note: The loader is a small script (about 10 KB) placed at the end of the page, so it does not delay your content."""

    return WidgetInstallCodeResponse(
        html_snippet=html_snippet,
        instructions=instructions,
    )


# Public endpoint for widget to fetch configuration
@router.get(
    "/public/config/{tenant_id}",
    response_model=PublicWidgetConfigResponse,
    status_code=status.HTTP_200_OK,
)
def get_public_widget_config(
    request: Request,
    tenant_id: str = Path(
        ...,
        description="Tenant UUID (not the tenant name/slug)",
        examples=["a1b2c3d4-5e6f-7g8h-9i0j-k1l2m3n4o5p6"],
    ),
    x_api_key: str = Header(..., description="Public API key"),
    x_visitor_id: str | None = Header(default=None, description="Anonymous visitor identifier"),
    db: Session = Depends(get_db),
):
    """
    Get widget configuration (public endpoint for embedded widget).

    This endpoint is called by the widget JavaScript running on customer websites.
    Requires a valid public API key.

    Args:
        tenant_id: Tenant UUID (not the tenant name/slug)
        x_api_key: Public API key (header)
        db: Database session

    Returns:
        Widget configuration for embedding

    Raises:
        HTTPException: 401 if API key is invalid
        HTTPException: 404 if tenant not found
    """
    # Verify API key
    api_key_service = APIKeyService(db)
    key_record = api_key_service.verify_api_key(x_api_key)

    if not key_record:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )

    # Get tenant
    try:
        tenant_uuid = UUID(tenant_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Tenant not found",
        ) from None
    stmt = select(Tenant).where(Tenant.id == tenant_uuid)
    tenant = db.execute(stmt).scalar_one_or_none()

    if not tenant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Tenant not found",
        )

    # Verify API key belongs to this tenant
    if key_record.tenant_id != tenant.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="API key does not belong to this tenant",
        )

    # Remember that the widget is live on a customer site (hostname only, throttled)
    record_widget_seen(db, tenant, request.headers.get("origin"), request.headers.get("referer"))

    # Track chat widget visit for platform analytics
    request_host = request.client.host if request and request.client else "unknown"
    visitor_key = (x_visitor_id or f"ip:{request_host}").strip()[:128]
    VisitAnalyticsService(db).record_visit(
        source="chat_widget",
        tenant_id=tenant.id,
        visitor_key=f"widget:{visitor_key}",
    )

    # Return public configuration
    return PublicWidgetConfigResponse(
        tenant_id=str(tenant.id),
        company_name=tenant.display_name,
        company_description=tenant.description,
        primary_color=tenant.widget_primary_color or "#2563eb",
        accent_color=tenant.widget_accent_color or "#10b981",
        logo_url=tenant.widget_logo_url,
        greeting_message=tenant.widget_greeting_message or "Hi! How can we help you today?",
        position=tenant.widget_position or "bottom-right",
        show_unread_badge=tenant.widget_show_unread_badge,
        allow_file_upload=tenant.widget_allow_file_upload,
        allow_screen_recording=screen_recording_allowed(db, tenant),
        collect_name_before_chat=bool(getattr(tenant.ai_config, "collect_name_before_chat", False)),
        collect_email_before_chat=bool(
            getattr(tenant.ai_config, "collect_email_before_chat", False)
        ),
        first_responder=getattr(tenant.ai_config, "first_responder", "ai") or "ai",
    )
