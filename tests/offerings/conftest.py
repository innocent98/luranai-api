"""Fakes for the offering photo tests: Cloudinary (uploader + destroy) and image moderation."""

import io
from unittest.mock import AsyncMock

import cloudinary.uploader
import pytest
from PIL import Image

from app.utils.cloudinary_client import cloudinary_manager


def make_image(fmt: str = "JPEG", size=(120, 90)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, "blue").save(buf, format=fmt)
    return buf.getvalue()


class FakeCloudinary:
    def __init__(self) -> None:
        self.uploads: list[dict] = []
        self.destroyed: list[str] = []
        self.fail_upload = False
        self.fail_destroy = False
        self.bytes = 240_000
        self._n = 0

    def upload(self, file, **kwargs):
        if self.fail_upload:
            raise RuntimeError("cloudinary down (secret-ish detail)")
        self._n += 1
        self.uploads.append({"size": len(file), **kwargs})
        folder = kwargs.get("folder")
        public_id = kwargs["public_id"]
        full = f"{folder}/{public_id}" if folder else public_id
        return {
            "secure_url": f"https://res.cloudinary.com/demo/image/upload/v1700000000/{full}.jpg",
            "public_id": full,
            "width": 1200,
            "height": 900,
            "bytes": self.bytes,
        }

    def destroy(self, public_id, **kwargs):
        self.destroyed.append(public_id)
        if self.fail_destroy:
            raise RuntimeError("destroy failed")
        return {"result": "ok"}


@pytest.fixture
def cloud(monkeypatch):
    fake = FakeCloudinary()
    monkeypatch.setattr(cloudinary.uploader, "upload", fake.upload)
    monkeypatch.setattr(cloudinary.uploader, "destroy", fake.destroy)
    monkeypatch.setattr(cloudinary_manager, "enabled", True)
    return fake


@pytest.fixture
def moderation(monkeypatch):
    mock = AsyncMock(return_value=(True, None))
    monkeypatch.setattr("app.services.offering_images.moderate_image", mock)
    return mock
