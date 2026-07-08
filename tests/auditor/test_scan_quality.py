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
