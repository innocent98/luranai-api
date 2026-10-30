"""Delivery renditions of an offering photo, derived by URL from the one stored asset.

Only the optimised copy is stored (see `CloudinaryManager.upload_offering_image`); the card
thumbnail, the gallery size and the WhatsApp copy are transformations of that asset, never
separate uploads, and all stay on the allow-listed Cloudinary host.
"""

import re

_UPLOAD = "/upload/"
_VERSION_SEGMENT = re.compile(r"^v\d+/")


def rendition(url: str, transformation: str) -> str:
    """Insert `transformation` after `/upload/`; leave anything else (or an already
    transformed URL) untouched."""
    head, sep, tail = url.partition(_UPLOAD)
    if not sep or not _VERSION_SEGMENT.match(tail):
        return url
    return f"{head}{sep}{transformation}/{tail}"


def thumb_url(url: str) -> str:
    """Card / list thumbnail, about 400 px on the long edge."""
    return rendition(url, "c_limit,w_400,q_auto,f_jpg")


def gallery_url(url: str) -> str:
    """Gallery size, about 1,000 px."""
    return rendition(url, "c_limit,w_1000,q_auto,f_jpg")


def whatsapp_url(url: str) -> str:
    """The copy sent to WhatsApp: at most 1 MB in practice (1,280 px, good quality JPEG)."""
    return rendition(url, "w_1280,q_auto:good,f_jpg")
