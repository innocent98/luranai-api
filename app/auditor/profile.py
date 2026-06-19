"""BusinessProfile synthesis via the LLM seam; persist + reuse-unless-changed."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.orm import Session

from app.auditor.extraction import ExtractedPage
from app.auditor.llm import complete_json
from app.db.models.business_profile import BusinessProfile, BusinessProfileSource

_SYSTEM = (
    "You are a business analyst. Read the website excerpts and return a JSON object describing "
    "the business. Keys: summary (string), industry (string), offerings (array of strings), "
    "audience (array of strings), positioning (string), key_entities (array of strings), "
    "tone (object), confidence (0-1 float). Make no currency assumptions."
)


def _build_sample(pages: list[ExtractedPage], max_sample_chars: int) -> str:
    parts: list[str] = []
    total = 0
    for page in pages:
        block = f"URL: {page.url}\nTITLE: {page.title or ''}\n{page.text}\n---\n"
        if total + len(block) > max_sample_chars:
            break
        parts.append(block)
        total += len(block)
    return "".join(parts)


async def synthesize_profile(pages: list[ExtractedPage], *, max_sample_chars: int = 12000) -> dict:
    """Call the LLM to synthesize a BusinessProfile dict from a token-budgeted page sample."""
    sample = _build_sample(pages, max_sample_chars)
    data = await complete_json("profile_synthesis", _SYSTEM, sample, temperature=0.1)
    return {
        "summary": data.get("summary"),
        "industry": data.get("industry"),
        "offerings": data.get("offerings") or [],
        "audience": data.get("audience") or [],
        "positioning": data.get("positioning"),
        "key_entities": data.get("key_entities") or [],
        "tone": data.get("tone") or {},
        "confidence": data.get("confidence"),
    }


def persist_profile(db: Session, tenant_id: UUID, data: dict, *, model_version: str) -> BusinessProfile:
    """Upsert one BusinessProfile row per tenant; bump refreshed_at on every call."""
    bp = db.query(BusinessProfile).filter(BusinessProfile.tenant_id == tenant_id).one_or_none()
    if bp is None:
        bp = BusinessProfile(tenant_id=tenant_id, source=BusinessProfileSource.AUDITOR)
        db.add(bp)
    bp.summary = data.get("summary")
    bp.industry = data.get("industry")
    bp.offerings = data.get("offerings") or []
    bp.audience = data.get("audience") or []
    bp.positioning = data.get("positioning")
    bp.key_entities = data.get("key_entities") or []
    bp.tone = data.get("tone") or {}
    bp.confidence = data.get("confidence")
    bp.model_version = model_version
    bp.raw = data
    bp.refreshed_at = datetime.now(UTC)
    return bp


def content_signature(pages: list[ExtractedPage]) -> str:
    """Stable SHA-256 of sorted content_hashes — order-independent."""
    joined = "|".join(sorted(p.content_hash for p in pages))
    return hashlib.sha256(joined.encode()).hexdigest()


async def synthesize_or_reuse(
    db: Session, tenant_id: UUID, pages: list[ExtractedPage], *, prior_signature: str | None
) -> tuple[BusinessProfile, str]:
    """Return (profile, sig). Reuses an existing persisted profile when signature is unchanged.

    The no-tenant (public pre-signup) case is not handled here — callers without a tenant_id
    should call synthesize_profile() directly and keep the result in memory.
    """
    sig = content_signature(pages)
    existing = db.query(BusinessProfile).filter(BusinessProfile.tenant_id == tenant_id).one_or_none()
    if existing is not None and prior_signature == sig:
        return existing, sig
    from app.auditor.llm import model_for_task

    data = await synthesize_profile(pages)
    bp = persist_profile(db, tenant_id, data, model_version=model_for_task("profile_synthesis"))
    return bp, sig
