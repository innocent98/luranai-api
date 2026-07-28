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

_RENDER_FAILED_REASON = "Something went wrong on our end while reading your site. Please try again."
_UNREACHABLE_REASON = "We couldn't reach your website. Check the URL and try again."
_THIN_REASON = (
    "We could only partially read your site — you may get a fuller picture by "
    "retrying or importing content manually."
)
_BLOCKED_REASON = (
    "This site's bot/firewall protection blocked our scanner. Ask them to "
    "allowlist Luran, or import your content manually."
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
    root_rejected: bool = False,
) -> tuple[ScanQuality, str]:
    """Return (quality, reason) for a completed crawl.

    Content-first: if the crawl produced usable content, the site was reachable
    AND rendered, regardless of the ROOT page's status — a 403/soft-404 homepage
    with readable subpages must not be flagged unreachable. Only when there is no
    usable content do we diagnose the failure from the root page.

    ``root_rejected`` says the guard refused the root URL, so the crawl never
    ran and there is no root_page to read. Without it, that case falls into the
    ``root_page is None`` branch and reports RENDER_FAILED — blaming our
    renderer for a domain that merely failed to resolve, and telling the user to
    "try again" on a URL that will never work.

    - OK:            total extracted content ≥ threshold.
    - THIN:          some content but below threshold (incl. rendered-but-empty root).
    - UNREACHABLE:   no content and the root timed out / errored / was blocked or
                     refused by the guard (unresolvable or unsafe host).
    - RENDER_FAILED: no content and our renderer couldn't launch (or no root page).
    """
    total_chars = sum(len(p.text) for p in extracted)
    if total_chars >= thin_min_content_chars:
        return ScanQuality.OK, ""
    if total_chars > 0:
        return ScanQuality.THIN, _THIN_REASON
    # No usable content — diagnose the failure.
    # The guard refusing the root is about THEIR url, not our renderer, so this
    # must be checked before the root_page-is-None branch below.
    if root_rejected:
        return ScanQuality.UNREACHABLE, _UNREACHABLE_REASON
    if root_page is not None and root_page.error_kind == RawPageError.SOFT_BLOCKED:
        return ScanQuality.BLOCKED, _BLOCKED_REASON
    if root_page is None or root_page.error_kind == RawPageError.LAUNCH_FAILED:
        return ScanQuality.RENDER_FAILED, _RENDER_FAILED_REASON
    if root_page.error_kind in _UNREACHABLE_KINDS:
        return ScanQuality.UNREACHABLE, _UNREACHABLE_REASON
    return ScanQuality.THIN, _THIN_REASON
