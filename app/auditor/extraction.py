"""Main-content extraction (trafilatura) + content_hash + dedup."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

import trafilatura
from bs4 import BeautifulSoup

from app.auditor.crawler import RawPage


@dataclass
class ExtractedPage:
    url: str
    title: str | None
    text: str
    content_hash: str


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def extract_main(html: str) -> tuple[str | None, str]:
    text = trafilatura.extract(html, include_comments=False, include_tables=True) or ""
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.string.strip() if soup.title and soup.title.string else None
    if not text:
        text = soup.get_text(separator=" ", strip=True)
    return title, text.strip()


def extract_pages(pages: list[RawPage]) -> list[ExtractedPage]:
    out: list[ExtractedPage] = []
    seen_hashes: set[str] = set()
    for page in pages:
        if page.error or not page.html:
            continue
        title, text = extract_main(page.html)
        if not text:
            continue
        h = content_hash(text)
        if h in seen_hashes:
            continue
        seen_hashes.add(h)
        out.append(ExtractedPage(url=page.url, title=title, text=text, content_hash=h))
    return out
