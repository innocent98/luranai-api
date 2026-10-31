"""Offering photo upload: shared validator arguments, incoming transformation, quality retry."""

import io
from unittest.mock import patch
from uuid import uuid4

import pytest
from PIL import Image

from app.core.config import settings
from app.utils.cloudinary_client import CloudinaryManager, offering_sweep_tag


def _image(fmt: str, size=(40, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, "red").save(buf, format=fmt)
    return buf.getvalue()


@pytest.fixture
def mgr():
    m = CloudinaryManager()
    m.enabled = True
    return m


def test_validate_image_defaults_are_unchanged_for_chat(mgr):
    assert mgr.validate_image(_image("GIF")) == (True, None)
    ok, err = mgr.validate_image(_image("PNG", (4001, 10)))
    assert ok is False and "exceed maximum" in err


def test_validate_image_can_restrict_formats(mgr):
    allowed = ["JPEG", "PNG", "WEBP"]
    assert mgr.validate_image(_image("JPEG"), allowed_formats=allowed) == (True, None)
    assert mgr.validate_image(_image("PNG"), allowed_formats=allowed) == (True, None)
    assert mgr.validate_image(_image("WEBP"), allowed_formats=allowed) == (True, None)
    ok, err = mgr.validate_image(_image("GIF"), allowed_formats=allowed)
    assert ok is False and "GIF" in err and "not allowed" in err


def test_validate_image_takes_a_larger_dimension_for_untouched_phone_photos(mgr):
    photo = _image("JPEG", (4032, 3024))
    assert mgr.validate_image(photo, max_dimension=9000) == (True, None)
    ok, _ = mgr.validate_image(_image("PNG", (9001, 10)), max_dimension=9000)
    assert ok is False


def test_upload_offering_image_sends_the_optimising_incoming_transformation(mgr):
    tenant_id = uuid4()
    fake = {
        "secure_url": "https://res.cloudinary.com/demo/image/upload/v1/luran/offerings/t/abc.jpg",
        "public_id": f"luran/offerings/{tenant_id}/abc",
        "width": 1600,
        "height": 1200,
        "bytes": 310_000,
    }
    with patch("cloudinary.uploader.upload", return_value=fake) as up:
        info, err = mgr.upload_offering_image(b"bytes", tenant_id)
    assert err is None
    assert info == {
        "url": fake["secure_url"],
        "public_id": fake["public_id"],
        "width": 1600,
        "height": 1200,
        "bytes": 310_000,
    }
    assert up.call_count == 1
    kwargs = up.call_args.kwargs
    assert kwargs["folder"] == f"{settings.CLOUDINARY_OFFERINGS_FOLDER}/{tenant_id}"
    # `image_metadata=False` only keeps metadata out of the upload RESPONSE; it strips nothing.
    assert kwargs["format"] == "jpg" and kwargs["image_metadata"] is False
    # What is asked of Cloudinary: shrink, re-encode and `strip_profile`. That EXIF/GPS really
    # disappears is Cloudinary's doing and is NOT verified here (see the SOP's staging checks).
    assert kwargs["transformation"] == [
        {
            "width": 1600,
            "height": 1600,
            "crop": "limit",
            "quality": "auto",
            "flags": "strip_profile",
        }
    ]
    assert kwargs["resource_type"] == "image" and kwargs["overwrite"] is False
    assert "offering" in kwargs["tags"] and offering_sweep_tag() in kwargs["tags"]
    assert str(tenant_id) in kwargs["tags"]


def test_a_result_over_800kb_is_reuploaded_once_at_lower_quality_over_the_same_asset(mgr):
    tenant_id = uuid4()
    big = {
        "secure_url": "https://res.cloudinary.com/d/image/upload/v1/x.jpg",
        "public_id": "luran/offerings/t/x",
        "width": 1600,
        "height": 1200,
        "bytes": 900_000,
    }
    small = {**big, "bytes": 420_000}
    with patch("cloudinary.uploader.upload", side_effect=[big, small]) as up:
        info, err = mgr.upload_offering_image(b"bytes", tenant_id)
    assert err is None and info["bytes"] == 420_000
    assert up.call_count == 2
    first, second = up.call_args_list[0].kwargs, up.call_args_list[1].kwargs
    assert first["transformation"][0]["quality"] == "auto"
    assert second["transformation"][0]["quality"] == 60
    assert second["public_id"] == "luran/offerings/t/x" and second["overwrite"] is True
    assert second["folder"] is None  # public_id already carries the folder


def test_the_retry_happens_at_most_once_even_if_still_large(mgr):
    big = {
        "secure_url": "https://res.cloudinary.com/d/image/upload/v1/x.jpg",
        "public_id": "luran/offerings/t/x",
        "width": 1600,
        "height": 1200,
        "bytes": 900_000,
    }
    with patch("cloudinary.uploader.upload", side_effect=[big, big]) as up:
        info, err = mgr.upload_offering_image(b"bytes", uuid4())
    assert up.call_count == 2 and err is None and info["bytes"] == 900_000


def test_upload_failure_is_an_error_not_a_raise(mgr):
    with patch("cloudinary.uploader.upload", side_effect=RuntimeError("boom with secret")):
        info, err = mgr.upload_offering_image(b"bytes", uuid4())
    assert info is None and err == "Failed to upload image"


def test_upload_offering_image_disabled(mgr):
    mgr.enabled = False
    info, err = mgr.upload_offering_image(b"bytes", uuid4())
    assert info is None and err == "Image upload is not configured"


def test_list_tagged_images_pages(mgr):
    page = {"resources": [{"public_id": "a"}], "next_cursor": "c2"}
    with patch("cloudinary.api.resources_by_tag", return_value=page) as api:
        res, cursor = mgr.list_tagged_images("offering_test", None)
    assert res == [{"public_id": "a"}] and cursor == "c2"
    assert api.call_args.kwargs["resource_type"] == "image"
