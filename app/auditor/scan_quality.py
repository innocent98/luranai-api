"""Classify the outcome of an audit crawl into an honest scan-quality state."""

from __future__ import annotations

from collections.abc import Sequence

from app.auditor.crawler import RawPage, RawPageError
from app.db.models.audit import ScanQuality

_UNREACHABLE_KINDS = {
    RawPageError.TIMEOUT,
    RawPageError.HTTP_ERROR,
    RawPageError.BLOCKED,
}

_RENDER_FAILED_REASON = (
    "Something went wrong on our end while reading your site. Please try again."
)
_UNREACHABLE_REASON = "We couldn't reach your website. Check the URL and try again."
_THIN_REASON = (
    "We could only partially read your site — you may get a fuller picture by "
    "retrying or importing content manually."
)


class ScanUnreadable(Exception):
    """Raised when a crawl produced no usable content (unreachable / render_failed).

    Carries the quality + reason so the pipeline can persist them before the
    report is marked FAILED.
    """

    def __init__(self, quality: ScanQuality, reason: str) -> None:
        self.quality = quality
        self.reason = reason
        super().__init__(reason)


def classify_scan_outcome(
    root_page: RawPage | None,
    extracted: Sequence,
    *,
    thin_min_content_chars: int,
) -> tuple[ScanQuality, str]:
    """Return (quality, reason) for a completed crawl.

    - RENDER_FAILED: our renderer could not launch, or the root page is missing.
    - UNREACHABLE:   the root page timed out, errored, or was SSRF-blocked.
    - THIN:          the root rendered but total extracted content is below the
                     threshold (e.g. only an empty SPA shell was captured).
    - OK:            otherwise.
    """
    if root_page is None or root_page.error_kind == RawPageError.LAUNCH_FAILED:
        return ScanQuality.RENDER_FAILED, _RENDER_FAILED_REASON
    if root_page.error_kind in _UNREACHABLE_KINDS:
        return ScanQuality.UNREACHABLE, _UNREACHABLE_REASON
    total_chars = sum(len(p.text) for p in extracted)
    if total_chars < thin_min_content_chars:
        return ScanQuality.THIN, _THIN_REASON
    return ScanQuality.OK, ""
