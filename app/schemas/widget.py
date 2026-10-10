"""Widget configuration Pydantic schemas."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


# Widget configuration schemas
class WidgetConfigUpdate(BaseModel):
    """Schema for updating widget configuration."""

    model_config = ConfigDict(extra="forbid")

    widget_primary_color: str | None = Field(
        None,
        pattern=r"^#[0-9A-Fa-f]{6}$",
        description="Primary color (hex format, e.g., #2563eb)",
    )
    widget_accent_color: str | None = Field(
        None,
        pattern=r"^#[0-9A-Fa-f]{6}$",
        description="Accent color (hex format, e.g., #10b981)",
    )
    widget_logo_url: str | None = Field(
        None,
        max_length=500,
        description="URL to company logo",
    )
    widget_greeting_message: str | None = Field(
        None,
        min_length=1,
        max_length=500,
        description="Greeting message shown in widget",
    )
    widget_position: str | None = Field(
        None,
        pattern=r"^(bottom-right|bottom-left|top-right|top-left)$",
        description="Widget position on page",
    )
    widget_show_unread_badge: bool | None = Field(
        None,
        description="Show unread message badge",
    )
    widget_allow_file_upload: bool | None = Field(
        None,
        description="Allow file uploads in chat",
    )


class WidgetConfigResponse(BaseModel):
    """Schema for widget configuration response."""

    model_config = ConfigDict(from_attributes=True)

    tenant_id: UUID
    company_name: str = Field(..., description="Company display name")
    widget_primary_color: str | None
    widget_accent_color: str | None
    widget_logo_url: str | None
    widget_greeting_message: str | None
    widget_position: str | None
    widget_show_unread_badge: bool
    widget_allow_file_upload: bool


# Public widget configuration (for widget to fetch)
class PublicWidgetConfigResponse(BaseModel):
    """Schema for public widget configuration (used by embedded widget)."""

    tenant_id: str = Field(..., description="Tenant identifier")
    company_name: str = Field(..., description="Company display name")
    company_description: str | None = Field(None, description="Company description")
    primary_color: str = Field(..., description="Primary color for widget")
    accent_color: str = Field(..., description="Accent color for widget")
    logo_url: str | None = Field(None, description="Company logo URL")
    greeting_message: str = Field(..., description="Greeting message")
    position: str = Field(..., description="Widget position")
    show_unread_badge: bool = Field(..., description="Show unread badge")
    allow_file_upload: bool = Field(..., description="Allow file uploads")
    collect_name_before_chat: bool = Field(..., description="Collect customer name before chat")
    collect_email_before_chat: bool = Field(..., description="Collect customer email before chat")
    first_responder: str = Field("ai", description="Who answers first: ai, ai_with_handoff, human")


# Widget installation code response
class WidgetInstallCodeResponse(BaseModel):
    """Schema for widget installation code."""

    html_snippet: str = Field(..., description="HTML code snippet to embed on website")
    instructions: str = Field(..., description="Installation instructions")
