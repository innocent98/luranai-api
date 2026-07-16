"""Tests for app/auditor/scan_quality.py — outcome classification."""

from app.auditor.crawler import RawPage, RawPageError
from app.auditor.scan_quality import classify_scan_outcome
from app.db.models.audit import ScanQuality


def _extracted(total_chars: int):
    """Return a list with one object exposing .text of the given length."""
    class _P:
        text = "x" * total_chars
    return [_P()] if total_chars else []


def _root(error_kind, html="<html>ok</html>"):
    return RawPage(url="https://x.com/", status=200 if error_kind == RawPageError.OK else 0,
                   html=html if error_kind == RawPageError.OK else None,
                   bytes_len=len(html), error_kind=error_kind)


def test_render_failed_when_root_launch_failed():
    q, reason = classify_scan_outcome(_root(RawPageError.LAUNCH_FAILED), _extracted(0),
                                      thin_min_content_chars=800)
    assert q == ScanQuality.RENDER_FAILED
    assert reason


def test_render_failed_when_root_missing():
    q, reason = classify_scan_outcome(None, _extracted(0), thin_min_content_chars=800)
    assert q == ScanQuality.RENDER_FAILED


def test_unreachable_when_root_timeout():
    q, reason = classify_scan_outcome(_root(RawPageError.TIMEOUT), _extracted(0),
                                      thin_min_content_chars=800)
    assert q == ScanQuality.UNREACHABLE
    assert reason


def test_unreachable_when_root_http_error():
    q, _ = classify_scan_outcome(_root(RawPageError.HTTP_ERROR), _extracted(0),
                                 thin_min_content_chars=800)
    assert q == ScanQuality.UNREACHABLE


def test_unreachable_when_root_blocked():
    q, _ = classify_scan_outcome(_root(RawPageError.BLOCKED), _extracted(0),
                                 thin_min_content_chars=800)
    assert q == ScanQuality.UNREACHABLE


def test_thin_when_root_ok_but_little_content():
    q, reason = classify_scan_outcome(_root(RawPageError.OK), _extracted(100),
                                      thin_min_content_chars=800)
    assert q == ScanQuality.THIN
    assert reason


def test_ok_when_root_ok_and_enough_content():
    q, reason = classify_scan_outcome(_root(RawPageError.OK), _extracted(1200),
                                      thin_min_content_chars=800)
    assert q == ScanQuality.OK
    assert reason == ""


def test_content_first_ok_despite_errored_root():
    # A 403/soft-404 homepage (HTTP_ERROR) but readable subpages → OK, not
    # UNREACHABLE: if we extracted usable content the site WAS reachable.
    q, _ = classify_scan_outcome(_root(RawPageError.HTTP_ERROR), _extracted(1200),
                                 thin_min_content_chars=800)
    assert q == ScanQuality.OK


# ---------------------------------------------------------------------------
# root_rejected: the guard refused the root, so the crawl never ran and there
# is no root_page to diagnose from. Without this signal the `root_page is None`
# branch below reports RENDER_FAILED — telling a user whose domain simply does
# not resolve that something broke on OUR end, and inviting them to retry
# forever. See tests/auditor/test_service_start.py for the submit-time gate
# that catches the common (typo) case before a job is ever queued.
# ---------------------------------------------------------------------------


def test_root_rejected_is_unreachable_not_render_failed():
    q, reason = classify_scan_outcome(
        None, _extracted(0), thin_min_content_chars=800, root_rejected=True
    )
    assert q == ScanQuality.UNREACHABLE
    assert "reach" in reason.lower()


def test_root_rejected_still_reports_content_when_some_was_read():
    """Defensive: content wins over the rejection signal, mirroring the
    content-first rule the rest of this function follows."""
    q, _ = classify_scan_outcome(
        None, _extracted(5000), thin_min_content_chars=800, root_rejected=True
    )
    assert q == ScanQuality.OK


def test_root_not_rejected_keeps_render_failed_for_missing_root():
    """The RENDER_FAILED branch must still fire for a genuine renderer failure."""
    q, _ = classify_scan_outcome(
        None, _extracted(0), thin_min_content_chars=800, root_rejected=False
    )
    assert q == ScanQuality.RENDER_FAILED
