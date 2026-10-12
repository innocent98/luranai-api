"""Cloudinary client utility for image upload and management."""

import io
import time
from uuid import UUID, uuid4

import cloudinary
import cloudinary.api
import cloudinary.exceptions
import cloudinary.uploader
import cloudinary.utils
from loguru import logger
from PIL import Image

from app.core.config import settings

VIDEO_EAGER = "c_limit,q_auto,vc_h264,w_1280/mp4"
_VIDEO_TRANSFORM = [{"crop": "limit", "quality": "auto", "video_codec": "h264", "width": 1280}]
_POSTER_TRANSFORM = [{"start_offset": "0", "width": 640, "crop": "limit"}]


def video_sweep_tag() -> str:
    """Environment-scoped tag so the unregistered-video sweep only sees this env's uploads."""
    return f"screen_recording_{settings.ENVIRONMENT}"


class CloudinaryManager:
    """Manager for Cloudinary image operations."""

    def __init__(self) -> None:
        """Initialize Cloudinary client with credentials."""
        if not all(
            [
                settings.CLOUDINARY_CLOUD_NAME,
                settings.CLOUDINARY_API_KEY,
                settings.CLOUDINARY_API_SECRET,
            ]
        ):
            logger.warning("Cloudinary credentials not configured. Image upload will be disabled.")
            self.enabled = False
        else:
            cloudinary.config(
                cloud_name=settings.CLOUDINARY_CLOUD_NAME,
                api_key=settings.CLOUDINARY_API_KEY,
                api_secret=settings.CLOUDINARY_API_SECRET,
                secure=True,
            )
            self.enabled = True
            logger.info(f"Cloudinary initialized: {settings.CLOUDINARY_CLOUD_NAME}")

    def validate_image(self, file_content: bytes, max_size_mb: int = 5) -> tuple[bool, str | None]:
        """
        Validate image file.

        Args:
            file_content: Image file bytes
            max_size_mb: Maximum file size in MB

        Returns:
            Tuple of (is_valid, error_message)
        """
        # Check file size
        file_size_mb = len(file_content) / (1024 * 1024)
        if file_size_mb > max_size_mb:
            return (
                False,
                f"File size ({file_size_mb:.2f}MB) exceeds maximum allowed ({max_size_mb}MB)",
            )

        # Validate it's a real image
        try:
            image = Image.open(io.BytesIO(file_content))
            image.verify()

            # Check format
            allowed_formats = ["JPEG", "PNG", "GIF", "WEBP"]
            if image.format not in allowed_formats:
                return (
                    False,
                    f"Image format '{image.format}' not allowed. Allowed: {', '.join(allowed_formats)}",
                )

            # Check dimensions (optional - prevent extremely large images)
            max_dimension = 4000
            if image.width > max_dimension or image.height > max_dimension:
                return (
                    False,
                    f"Image dimensions ({image.width}x{image.height}) exceed maximum ({max_dimension}x{max_dimension})",
                )

            return True, None

        except Exception as e:
            logger.error(f"Image validation failed: {e}")
            return False, f"Invalid image file: {str(e)}"

    def upload_widget_logo(
        self,
        file_content: bytes,
        tenant_id: UUID,
        filename: str,
    ) -> tuple[str | None, str | None]:
        """
        Upload widget logo to Cloudinary.

        Args:
            file_content: Image file bytes
            tenant_id: Tenant UUID for organizing uploads
            filename: Original filename

        Returns:
            Tuple of (cloudinary_url, error_message)
        """
        if not self.enabled:
            return None, "Cloudinary is not configured. Please add credentials to .env"

        # Validate image
        is_valid, error = self.validate_image(file_content)
        if not is_valid:
            return None, error

        try:
            # Upload to Cloudinary with transformations
            upload_result = cloudinary.uploader.upload(
                file_content,
                folder=settings.CLOUDINARY_FOLDER,
                public_id=f"{tenant_id}_logo",  # Overwrites existing logo for tenant
                overwrite=True,
                resource_type="image",
                # Transformations for optimization
                transformation=[
                    {"width": 500, "height": 500, "crop": "limit"},  # Max 500x500
                    {"quality": "auto:good"},  # Auto-optimize quality
                    {"fetch_format": "auto"},  # Auto-select best format (WebP, JPEG, etc.)
                ],
                # Tags for organization
                tags=[str(tenant_id), "widget_logo"],
            )

            # Extract secure URL
            secure_url = upload_result.get("secure_url")
            logger.info(f"Uploaded widget logo for tenant {tenant_id}: {secure_url}")

            return secure_url, None

        except Exception as e:
            logger.error(f"Cloudinary upload failed: {e}")
            return None, f"Image upload failed: {str(e)}"

    def delete_widget_logo(self, tenant_id: UUID) -> tuple[bool, str | None]:
        """
        Delete widget logo from Cloudinary.

        Args:
            tenant_id: Tenant UUID

        Returns:
            Tuple of (success, error_message)
        """
        if not self.enabled:
            return False, "Cloudinary is not configured"

        try:
            public_id = f"{settings.CLOUDINARY_FOLDER}/{tenant_id}_logo"
            result = cloudinary.uploader.destroy(public_id)

            if result.get("result") == "ok":
                logger.info(f"Deleted widget logo for tenant {tenant_id}")
                return True, None
            else:
                return False, "Logo not found or already deleted"

        except Exception as e:
            logger.error(f"Cloudinary delete failed: {e}")
            return False, f"Failed to delete logo: {str(e)}"

    def upload_platform_email_image(
        self,
        file_content: bytes,
        admin_id: UUID,
        filename: str,
    ) -> tuple[str | None, str | None]:
        """
        Upload a platform email image to Cloudinary.

        Args:
            file_content: Image file bytes
            admin_id: Platform admin UUID for tracing uploads
            filename: Original filename

        Returns:
            Tuple of (cloudinary_url, error_message)
        """
        if not self.enabled:
            return None, "Cloudinary is not configured. Please add credentials to .env"

        is_valid, error = self.validate_image(file_content, max_size_mb=8)
        if not is_valid:
            return None, error

        safe_filename = filename.rsplit(".", 1)[0].strip().replace(" ", "_") or "campaign_image"
        public_id = f"{admin_id}_{safe_filename}_{uuid4().hex[:12]}"

        try:
            upload_result = cloudinary.uploader.upload(
                file_content,
                folder=settings.CLOUDINARY_PLATFORM_EMAIL_FOLDER,
                public_id=public_id,
                overwrite=False,
                resource_type="image",
                transformation=[
                    {"width": 1600, "height": 1600, "crop": "limit"},
                    {"quality": "auto:good"},
                    {"fetch_format": "auto"},
                ],
                tags=[str(admin_id), "platform_email_image"],
            )

            secure_url = upload_result.get("secure_url")
            logger.info(f"Uploaded platform email image for admin {admin_id}: {secure_url}")
            return secure_url, None
        except Exception as e:
            logger.error(f"Platform email image upload failed: {e}")
            return None, f"Image upload failed: {str(e)}"

    def upload_channel_image(
        self,
        file_content: bytes,
        tenant_id: UUID,
        filename: str = "channel_image",
    ) -> tuple[str | None, str | None]:
        """Upload a channel post image to Cloudinary."""
        if not self.enabled:
            return None, "Cloudinary is not configured. Please add credentials to .env"

        try:
            is_valid, error = self.validate_image(file_content, max_size_mb=5)
            if not is_valid:
                return None, error

            folder = f"{settings.CLOUDINARY_FOLDER or 'luran'}/channels/{tenant_id}"
            public_id = f"{folder}/{filename}_{int(__import__('time').time())}"

            result = cloudinary.uploader.upload(
                file_content,
                public_id=public_id,
                folder=None,
                overwrite=True,
                resource_type="image",
                transformation=[
                    {
                        "width": 1200,
                        "height": 630,
                        "crop": "limit",
                        "quality": "auto",
                        "fetch_format": "auto",
                    }
                ],
            )
            return result.get("secure_url"), None

        except Exception as e:
            logger.error(f"Channel image upload failed: {e}")
            return None, f"Upload failed: {str(e)}"

    def upload_chat_image(
        self,
        file_content: bytes,
        tenant_id: UUID,
        session_id: UUID,
        filename: str,
    ) -> tuple[str | None, str | None, str | None]:
        """Upload a chat image; returns (secure_url, public_id, error)."""
        if not self.enabled:
            return None, None, "Image upload is not configured"

        try:
            folder = f"{settings.CLOUDINARY_CHAT_FOLDER}/{tenant_id}/{session_id}"
            result = cloudinary.uploader.upload(
                file_content,
                folder=folder,
                public_id=uuid4().hex,
                overwrite=False,
                resource_type="image",
                # HEIC -> jpg, strip metadata (EXIF/GPS), cap dimensions
                format="jpg",
                transformation=[{"width": 2000, "height": 2000, "crop": "limit"}],
                image_metadata=False,
                tags=["chat", str(tenant_id)],
            )
            return result.get("secure_url"), result.get("public_id"), None
        except Exception as e:
            logger.error(f"Chat image upload failed: {e}")
            return None, None, "Failed to upload image"

    def destroy(self, public_id: str, *, resource_type: str) -> tuple[bool, str | None]:
        """Delete an asset; resource_type ("image"|"video") is required. 'not found' = success."""
        if not self.enabled:
            return False, "Cloudinary not configured"
        try:
            res = cloudinary.uploader.destroy(
                public_id, resource_type=resource_type, invalidate=True
            )
            if res.get("result") in ("ok", "not found"):
                return True, None
            return False, f"Failed to delete {resource_type}"
        except Exception as e:
            logger.error(f"Cloudinary destroy failed for {public_id}: {type(e).__name__}")
            return False, f"Failed to delete {resource_type}"

    def video_folder(self, tenant_id: UUID, session_id: UUID) -> str:
        return f"{settings.CLOUDINARY_CHAT_FOLDER}/{tenant_id}/{session_id}/video"

    def sign_video_upload(self, tenant_id: UUID, session_id: UUID) -> dict | None:
        """Signed direct-upload payload for a screen recording (secret never leaves the server).

        Every signed field is tamper-proof: folder scopes the asset to this
        tenant/session; tags let the unregistered-upload sweep find it.
        """
        if not self.enabled:
            return None
        to_sign = {
            "timestamp": int(time.time()),
            "folder": self.video_folder(tenant_id, session_id),
            "public_id": uuid4().hex,
            "eager": VIDEO_EAGER,
            "eager_async": "true",
            "overwrite": "false",  # no re-upload to a public_id after register validated it
            "tags": f"chat,screen_recording,{video_sweep_tag()},{tenant_id}",
        }
        signature = cloudinary.utils.api_sign_request(to_sign, settings.CLOUDINARY_API_SECRET)
        return {
            "upload_url": (
                f"https://api.cloudinary.com/v1_1/{settings.CLOUDINARY_CLOUD_NAME}/video/upload"
            ),
            "fields": {**to_sign, "api_key": settings.CLOUDINARY_API_KEY, "signature": signature},
            "public_id": f"{to_sign['folder']}/{to_sign['public_id']}",
        }

    def get_video_resource(self, public_id: str) -> dict | None:
        """Admin API lookup of an uploaded video; None if it does not exist."""
        try:
            # media_metadata=True is required: the plain Admin API resource omits `duration`,
            # which made every real registration fail the duration check.
            return cloudinary.api.resource(public_id, resource_type="video", media_metadata=True)
        except cloudinary.exceptions.NotFound:
            return None

    def video_delivery_urls(self, public_id: str, version: int | str | None) -> tuple[str, str]:
        """(progressive mp4 matching the signed eager, jpg poster at 0 s)."""
        mp4, _ = cloudinary.utils.cloudinary_url(
            public_id,
            resource_type="video",
            cloud_name=settings.CLOUDINARY_CLOUD_NAME,
            format="mp4",
            secure=True,
            version=version,
            transformation=_VIDEO_TRANSFORM,
        )
        poster, _ = cloudinary.utils.cloudinary_url(
            public_id,
            resource_type="video",
            cloud_name=settings.CLOUDINARY_CLOUD_NAME,
            format="jpg",
            secure=True,
            version=version,
            transformation=_POSTER_TRANSFORM,
        )
        return mp4, poster

    def list_tagged_videos(
        self, tag: str, next_cursor: str | None
    ) -> tuple[list[dict], str | None]:
        kwargs: dict = {"resource_type": "video", "max_results": 100}
        if next_cursor:
            kwargs["next_cursor"] = next_cursor
        page = cloudinary.api.resources_by_tag(tag, **kwargs)
        return page.get("resources", []), page.get("next_cursor")


# Global instance
cloudinary_manager = CloudinaryManager()
