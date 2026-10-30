"""Rendition URLs: card thumbnail, gallery and WhatsApp copies are derived from the stored asset."""

from app.services.offering_media import (
    gallery_url,
    rendition,
    thumb_url,
    whatsapp_url,
)

STORED = "https://res.cloudinary.com/demo/image/upload/v1700000000/luran/offerings/t1/abc.jpg"


def test_thumbnail_is_400px_limit_crop():
    assert thumb_url(STORED) == (
        "https://res.cloudinary.com/demo/image/upload/c_limit,w_400,q_auto,f_jpg/"
        "v1700000000/luran/offerings/t1/abc.jpg"
    )


def test_gallery_is_1000px():
    assert "/upload/c_limit,w_1000,q_auto,f_jpg/" in gallery_url(STORED)


def test_whatsapp_copy_is_1280px_good_quality_jpg():
    assert "/upload/w_1280,q_auto:good,f_jpg/" in whatsapp_url(STORED)
    assert whatsapp_url(STORED).startswith("https://res.cloudinary.com/")  # allow-listed host


def test_a_url_that_is_not_a_cloudinary_upload_is_returned_unchanged():
    other = "https://example.com/a.jpg"
    assert thumb_url(other) == other
    assert rendition(other, "w_10") == other


def test_a_url_already_carrying_a_transformation_is_not_double_transformed():
    once = thumb_url(STORED)
    assert thumb_url(once) == once
