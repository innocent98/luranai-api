from unittest.mock import patch
from uuid import uuid4

from app.utils.cloudinary_client import CloudinaryManager


def test_upload_chat_image_returns_url_and_public_id():
    m = CloudinaryManager()
    m.enabled = True
    fake = {
        "secure_url": "https://res.cloudinary.com/x/chat/t/s/abc.jpg",
        "public_id": "luran/chat/t/s/abc",
    }
    with patch("cloudinary.uploader.upload", return_value=fake):
        url, public_id, err = m.upload_chat_image(b"bytes", uuid4(), uuid4(), "p.jpg")
    assert err is None and url == fake["secure_url"] and public_id == fake["public_id"]


def test_destroy_treats_not_found_as_success():
    m = CloudinaryManager()
    m.enabled = True
    with patch("cloudinary.uploader.destroy", return_value={"result": "not found"}):
        ok, err = m.destroy("luran/chat/t/s/missing")
    assert ok is True and err is None


def test_upload_chat_image_disabled_returns_error():
    m = CloudinaryManager()
    m.enabled = False
    url, public_id, err = m.upload_chat_image(b"b", uuid4(), uuid4(), "p.jpg")
    assert url is None and public_id is None and err is not None
