from app.auditor import extraction
from app.auditor.crawler import RawPage


def test_content_hash_is_sha256_hex():
    h = extraction.content_hash("hello world")
    assert len(h) == 64 and all(c in "0123456789abcdef" for c in h)


def test_extract_main_returns_title_and_text():
    html = "<html><head><title>About Us</title></head><body><article><p>We sell widgets in Lagos.</p></article></body></html>"
    title, text = extraction.extract_main(html)
    assert title == "About Us"
    assert "widgets" in text


def test_extract_pages_dedupes_identical_content():
    html = "<html><head><title>T</title></head><body><article><p>Same body content here for dedup.</p></article></body></html>"
    pages = [
        RawPage(url="https://x.com/a", status=200, html=html, bytes_len=10),
        RawPage(url="https://x.com/b", status=200, html=html, bytes_len=10),
        RawPage(url="https://x.com/err", status=0, html=None, bytes_len=0, error="TIMEOUT"),
    ]
    out = extraction.extract_pages(pages)
    assert len(out) == 1  # duplicate + error dropped
    assert out[0].content_hash == extraction.content_hash(out[0].text)


def test_content_hash_deterministic():
    h1 = extraction.content_hash("some text")
    h2 = extraction.content_hash("some text")
    assert h1 == h2


def test_content_hash_differs_for_different_text():
    h1 = extraction.content_hash("hello")
    h2 = extraction.content_hash("world")
    assert h1 != h2


def test_extract_main_empty_html_no_raise():
    title, text = extraction.extract_main("")
    assert title is None
    assert isinstance(text, str)


def test_extract_main_garbage_html_no_raise():
    title, text = extraction.extract_main("<<< NOT HTML >>>!!! @#$%")
    assert title is None
    assert isinstance(text, str)


def test_extract_pages_skips_error_pages():
    pages = [
        RawPage(url="https://x.com/err", status=0, html=None, bytes_len=0, error="TIMEOUT"),
        RawPage(url="https://x.com/err2", status=0, html=None, bytes_len=0, error="ROBOTS_BLOCKED"),
    ]
    out = extraction.extract_pages(pages)
    assert out == []


def test_extract_pages_different_content_both_included():
    html_a = "<html><head><title>A</title></head><body><article><p>Content of page alpha here.</p></article></body></html>"
    html_b = "<html><head><title>B</title></head><body><article><p>Content of page beta is different.</p></article></body></html>"
    pages = [
        RawPage(url="https://x.com/a", status=200, html=html_a, bytes_len=len(html_a)),
        RawPage(url="https://x.com/b", status=200, html=html_b, bytes_len=len(html_b)),
    ]
    out = extraction.extract_pages(pages)
    assert len(out) == 2


def test_extracted_page_has_correct_fields():
    html = "<html><head><title>My Page</title></head><body><article><p>Some real content lives here.</p></article></body></html>"
    pages = [RawPage(url="https://x.com/p", status=200, html=html, bytes_len=len(html))]
    out = extraction.extract_pages(pages)
    assert len(out) == 1
    page = out[0]
    assert page.url == "https://x.com/p"
    assert page.title == "My Page"
    assert isinstance(page.text, str)
    assert len(page.content_hash) == 64


def test_extract_pages_skips_non_2xx_error_pages():
    """A 404/403 body (error_kind HTTP_ERROR) must be skipped even though it has
    html and no `error` string — it must not pollute the KB or inflate scores."""
    from app.auditor.crawler import RawPageError

    ok_html = "<html><head><title>OK</title></head><body><article><p>Real product page content.</p></article></body></html>"
    err_html = "<html><head><title>404</title></head><body><article><p>Page not found sorry.</p></article></body></html>"
    pages = [
        RawPage(url="https://x.com/ok", status=200, html=ok_html, bytes_len=len(ok_html),
                error_kind=RawPageError.OK),
        RawPage(url="https://x.com/missing", status=404, html=err_html, bytes_len=len(err_html),
                error_kind=RawPageError.HTTP_ERROR),
    ]
    out = extraction.extract_pages(pages)
    urls = [p.url for p in out]
    assert "https://x.com/ok" in urls
    assert "https://x.com/missing" not in urls
