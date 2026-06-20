"""Adaptive per-page classification + normalized-kind -> source_type map."""

from __future__ import annotations

from dataclasses import dataclass

from app.auditor.extraction import ExtractedPage
from app.auditor.llm import complete_json
from app.db.models.audit import AuditEntryKind
from app.db.models.knowledge import SourceType

KIND_TO_SOURCE_TYPE: dict[AuditEntryKind, SourceType] = {
    AuditEntryKind.FAQ: SourceType.FAQ,
    AuditEntryKind.GUIDE: SourceType.HELP_DOC,
    AuditEntryKind.POLICY: SourceType.POLICY,
    AuditEntryKind.PRODUCT: SourceType.OTHER,
    AuditEntryKind.PRICING: SourceType.OTHER,
    AuditEntryKind.CONTACT: SourceType.OTHER,
    AuditEntryKind.TESTIMONIAL: SourceType.OTHER,
    AuditEntryKind.OTHER: SourceType.OTHER,
}


def map_kind(kind: AuditEntryKind) -> SourceType:
    """Return the RAG SourceType for a given AuditEntryKind.

    All AuditEntryKind values are present in KIND_TO_SOURCE_TYPE, so this
    will never raise for valid enum members.
    """
    return KIND_TO_SOURCE_TYPE[kind]


@dataclass
class Classification:
    category: str
    kind: AuditEntryKind
    importance_rank: int
    keep: bool


_SYSTEM = (
    "You classify a single web page for a specific business. Given the business profile and the page, "
    "return JSON: category (free-form business-specific label, string), kind (one of "
    "faq|policy|product|pricing|guide|contact|testimonial|other), importance_rank (1=most important), "
    "keep (boolean — false for irrelevant/boilerplate pages). Adapt to THIS business; no fixed taxonomy."
)


def _coerce_kind(raw: str) -> AuditEntryKind:
    try:
        return AuditEntryKind(str(raw).lower())
    except ValueError:
        return AuditEntryKind.OTHER


async def classify_page(page: ExtractedPage, profile: dict) -> Classification:
    """Classify a single page using the LLM with the business profile as context."""
    user = (
        f"BUSINESS PROFILE: {profile}\n\nPAGE URL: {page.url}\nPAGE TITLE: {page.title or ''}\n"
        f"PAGE TEXT:\n{page.text[:4000]}"
    )
    data = await complete_json("page_classify", _SYSTEM, user, temperature=0.1)
    return Classification(
        category=str(data.get("category") or "uncategorized"),
        kind=_coerce_kind(data.get("kind", "other")),
        importance_rank=int(data.get("importance_rank") or 99),
        keep=bool(data.get("keep", True)),
    )


async def classify_pages(
    pages: list[ExtractedPage], profile: dict
) -> list[tuple[ExtractedPage, Classification]]:
    """Classify all pages and return only those marked keep=True."""
    out: list[tuple[ExtractedPage, Classification]] = []
    for page in pages:
        c = await classify_page(page, profile)
        if c.keep:
            out.append((page, c))
    return out
