"""Why a knowledge source or ingestion job failed, in words a tenant can act on.

The codes are part of the API contract (`failure_reason: {code, message}` on knowledge sources
and job status, and the flat `{code, message}` body of the upload errors).
"""

import enum
from dataclasses import dataclass

# Anything shorter than this (after trimming) cannot be answered from; reject it up front.
MIN_DOCUMENT_CHARS = 20
# Pasted text is capped at the same 10 MB as file uploads (one character is >= 1 byte).
MAX_DOCUMENT_CHARS = 10 * 1024 * 1024


class FailureCode(str, enum.Enum):
    DOCUMENT_TOO_SHORT = "document_too_short"
    NO_TEXT_EXTRACTED = "no_text_extracted"
    EMBEDDING_FAILED = "embedding_failed"
    VECTOR_STORE_UNAVAILABLE = "vector_store_unavailable"


FAILURE_MESSAGES: dict[str, str] = {
    FailureCode.DOCUMENT_TOO_SHORT.value: (
        f"This document is too short. Add at least {MIN_DOCUMENT_CHARS} characters of text so the "
        "AI has something to answer from."
    ),
    FailureCode.NO_TEXT_EXTRACTED.value: (
        "We could not find any readable text in this document. If it is a scanned PDF or an "
        "image, upload a text version or paste the text instead."
    ),
    FailureCode.EMBEDDING_FAILED.value: (
        "We could not prepare this document for the AI right now. Please try uploading it again "
        "in a few minutes."
    ),
    FailureCode.VECTOR_STORE_UNAVAILABLE.value: (
        "We could not save this document to the AI's knowledge store right now. Please try "
        "uploading it again in a few minutes."
    ),
}

# HTTP status for the synchronous text upload when a code is raised as an error.
FAILURE_HTTP_STATUS: dict[str, int] = {
    FailureCode.DOCUMENT_TOO_SHORT.value: 422,
    FailureCode.NO_TEXT_EXTRACTED.value: 422,
    FailureCode.EMBEDDING_FAILED.value: 502,
    FailureCode.VECTOR_STORE_UNAVAILABLE.value: 503,
}


def failure_message(code: str | None) -> str | None:
    if code is None:
        return None
    return FAILURE_MESSAGES.get(code, "This document could not be processed.")


@dataclass
class FailureReason:
    code: str
    message: str


def failure_reason(code: str | None) -> FailureReason | None:
    if not code:
        return None
    return FailureReason(code=code, message=failure_message(code) or "")


class KnowledgeError(Exception):
    """Mapped to a flat `{code, message[, min_chars]}` JSON body by the registered handler."""

    def __init__(self, code: FailureCode) -> None:
        self.failure = code
        self.code = code.value
        self.message = FAILURE_MESSAGES[code.value]
        self.status_code = FAILURE_HTTP_STATUS[code.value]
        self.extra: dict = (
            {"min_chars": MIN_DOCUMENT_CHARS} if code is FailureCode.DOCUMENT_TOO_SHORT else {}
        )
        super().__init__(self.message)

    def body(self) -> dict:
        return {"code": self.code, "message": self.message, **self.extra}


def check_document_text(text: str | None) -> FailureCode | None:
    """Return the failure code for text that cannot become knowledge, else None."""
    stripped = (text or "").strip()
    if len(stripped) < MIN_DOCUMENT_CHARS:
        return FailureCode.DOCUMENT_TOO_SHORT
    if not any(ch.isalnum() for ch in stripped):
        return FailureCode.NO_TEXT_EXTRACTED
    return None
