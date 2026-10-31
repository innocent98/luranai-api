"""The "not found" log (spec 6, 13): what customers asked for that is not listed.

Stores a normalised term only: no session, customer or raw message. Emails, URLs and number runs
are removed before anything is read, so a phone number typed next to the question never lands here.
"""

import logging
import re
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models.offering import OfferingMiss
from app.offerings.grounding import tokenize

logger = logging.getLogger(__name__)

TERM_MAX = 80
_NOISE = re.compile(r"\S+@\S+|https?://\S+|www\.\S+|\+?\d[\d\s().-]{5,}\d")
_ASK = re.compile(
    r"(?:do\s+you\s+(?:sell|have|stock|do|offer)|have\s+you\s+got|you\s+(?:sell|stock)|"
    r"i\s+(?:want|need|would\s+like|am\s+looking\s+for|'m\s+looking\s+for)|looking\s+for|"
    r"how\s+much\s+(?:is|are|for)|price\s+of|cost\s+of|buy|get\s+me|order)\s+"
    r"(?:an?\s+|the\s+|some\s+|any\s+|your\s+)?"
    r"([a-z0-9][a-z0-9' -]{1,40}?)(?=\s+(?:and|or|but|please|pls|my|for|in|at|so|because)\b|[?.!,;]|$)",
    re.IGNORECASE,
)


def normalise_term(message: str) -> str | None:
    text = _NOISE.sub(" ", message or "")
    m = _ASK.search(text)
    if not m:
        return None
    tokens = [t for t in tokenize(m.group(1)) if not t.isdigit() and len(t) >= 3][:4]
    term = " ".join(tokens)[:TERM_MAX].strip()
    return term or None


def _upsert(db: Session, tenant_id: UUID, term: str) -> None:
    now = datetime.now(UTC)
    for _attempt in range(2):
        try:
            with db.begin_nested():
                row = db.execute(
                    select(OfferingMiss).where(
                        OfferingMiss.tenant_id == tenant_id, OfferingMiss.term == term
                    )
                ).scalar_one_or_none()
                if row is None:
                    db.add(
                        OfferingMiss(
                            tenant_id=tenant_id,
                            term=term,
                            count=1,
                            first_seen_at=now,
                            last_seen_at=now,
                        )
                    )
                else:
                    row.count += 1
                    row.last_seen_at = now
            db.commit()
            return
        except IntegrityError:  # a concurrent first sighting of the same term: count it next pass
            continue


def record_miss(db: Session, tenant_id: UUID, message: str) -> None:
    """Never raises: the log is a convenience, the reply must not depend on it."""
    try:
        term = normalise_term(message)
        if term:
            _upsert(db, tenant_id, term)
    except Exception:  # noqa: BLE001
        logger.exception("offerings miss log failed (non-fatal)")
