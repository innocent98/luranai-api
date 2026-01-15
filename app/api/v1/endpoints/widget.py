"""Widget configuration and public endpoints."""

from fastapi import APIRouter, Depends, HTTPException, status, Header
from sqlalchemy.orm import Session
from sqlalchemy import select

from app.api.deps import get_current_tenant_admin, get_db
from app.db.models.tenant import Tenant
from app.db.models.api_key import APIKey
from app.services.api_key_service import APIKeyService
from app.schemas.widget import (
    WidgetConfigUpdate,
    WidgetConfigResponse,
    PublicWidgetConfigResponse,
    WidgetInstallCodeResponse,
)
from app.schemas.user import UserResponse

router = APIRouter()


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
    )


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

    db.commit()
    db.refresh(tenant)

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
    )


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
    html_snippet = f"""<!-- Luran Widget Integration -->
<script>
  window.luranSettings = {{
    tenantId: '{tenant.name}',
    apiKey: 'YOUR_PUBLIC_API_KEY_HERE'  // Get from API Keys page
  }};
</script>
<script src="https://cdn.luran.com/widget/v1/widget.js" async defer></script>
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

Note: The widget loads asynchronously and won't affect your page load speed."""

    return WidgetInstallCodeResponse(
        html_snippet=html_snippet,
        instructions=instructions,
    )


# Public endpoint for widget to fetch configuration
@router.get(
    "/public/config/{tenant_name}",
    response_model=PublicWidgetConfigResponse,
    status_code=status.HTTP_200_OK,
)
def get_public_widget_config(
    tenant_name: str,
    x_api_key: str = Header(..., description="Public API key"),
    db: Session = Depends(get_db),
):
    """
    Get widget configuration (public endpoint for embedded widget).

    This endpoint is called by the widget JavaScript running on customer websites.
    Requires a valid public API key.

    Args:
        tenant_name: Tenant identifier
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
    stmt = select(Tenant).where(Tenant.name == tenant_name)
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

    # Return public configuration
    return PublicWidgetConfigResponse(
        tenant_id=str(tenant.id),
        company_name=tenant.display_name,
        primary_color=tenant.widget_primary_color or "#2563eb",
        accent_color=tenant.widget_accent_color or "#10b981",
        logo_url=tenant.widget_logo_url,
        greeting_message=tenant.widget_greeting_message or "Hi! How can we help you today?",
        position=tenant.widget_position or "bottom-right",
        show_unread_badge=tenant.widget_show_unread_badge,
        allow_file_upload=tenant.widget_allow_file_upload,
    )
