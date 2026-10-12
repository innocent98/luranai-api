"""Video helpers on CloudinaryManager. The SDK is mocked; no network."""

from unittest.mock import patch
from uuid import uuid4

import cloudinary.utils
import pytest

from app.utils import cloudinary_client as cc


@pytest.fixture
def mgr(monkeypatch):
    m = cc.CloudinaryManager.__new__(cc.CloudinaryManager)
    m.enabled = True
    monkeypatch.setattr(cc.settings, "CLOUDINARY_API_SECRET", "test-secret-not-real")
    monkeypatch.setattr(cc.settings, "CLOUDINARY_API_KEY", "test-key")
    monkeypatch.setattr(cc.settings, "CLOUDINARY_CLOUD_NAME", "demo")
    monkeypatch.setattr(cc.settings, "CLOUDINARY_CHAT_FOLDER", "luran/chat_images")
    return m


def test_destroy_passes_resource_type(mgr):
    with patch.object(cc.cloudinary.uploader, "destroy", return_value={"result": "not found"}) as d:
        assert mgr.destroy("a/b", resource_type="video") == (True, None)
    d.assert_called_once_with("a/b", resource_type="video", invalidate=True)


def test_destroy_requires_resource_type(mgr):
    with pytest.raises(TypeError):
        mgr.destroy("a/b")  # type: ignore[call-arg]


def test_sign_video_upload_fields_and_signature(mgr):
    t, s = uuid4(), uuid4()
    out = mgr.sign_video_upload(t, s)
    f = out["fields"]
    assert out["upload_url"] == "https://api.cloudinary.com/v1_1/demo/video/upload"
    assert f["folder"] == f"luran/chat_images/{t}/{s}/video"
    assert f["eager"] == cc.VIDEO_EAGER and f["eager_async"] == "true"
    assert f["tags"] == f"chat,screen_recording,{t}"
    assert out["public_id"] == f"{f['folder']}/{f['public_id']}"
    signed = {
        k: f[k]
        for k in ("timestamp", "folder", "public_id", "eager", "eager_async", "overwrite", "tags")
    }
    assert f["overwrite"] == "false"
    assert f["signature"] == cloudinary.utils.api_sign_request(signed, "test-secret-not-real")
    tampered = {**signed, "folder": "luran/chat_images/other/video"}
    assert f["signature"] != cloudinary.utils.api_sign_request(tampered, "test-secret-not-real")
    assert "test-secret-not-real" not in repr(out)


def test_get_video_resource_not_found_returns_none(mgr):
    with patch.object(
        cc.cloudinary.api, "resource", side_effect=cc.cloudinary.exceptions.NotFound("x")
    ):
        assert mgr.get_video_resource("a/b") is None


def test_delivery_urls(mgr):
    mp4, poster = mgr.video_delivery_urls("luran/chat_images/t/s/video/abc", 123)
    assert mp4.startswith("https://res.cloudinary.com/demo/video/upload/")
    assert (
        "vc_h264" in mp4
        and "w_1280" in mp4
        and mp4.endswith("/v123/luran/chat_images/t/s/video/abc.mp4")
    )
    assert "so_0" in poster and "w_640" in poster and poster.endswith(".jpg")


def test_list_tagged_videos(mgr):
    page = {
        "resources": [{"public_id": "x", "created_at": "2026-10-10T00:00:00Z"}],
        "next_cursor": "c2",
    }
    with patch.object(cc.cloudinary.api, "resources_by_tag", return_value=page) as r:
        res, cursor = mgr.list_tagged_videos("screen_recording", None)
    assert res[0]["public_id"] == "x" and cursor == "c2"
    r.assert_called_once_with("screen_recording", resource_type="video", max_results=100)
