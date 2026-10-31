"""What the visitor is told about the quote email. Code writes it, the model never does.

The model cannot send email, so any "I'll email it to you" it produces is a promise nothing
keeps. `strip_email_promises` removes those sentences from every reply; `finalize_reply` then
adds the one sentence that is true (written here, with a masked address) when the platform did
queue a send, or could not.

Dependency-free: safe to import from the chat handler, the worker and tests.
"""

import re
from dataclasses import dataclass

_FALLBACK = "Let me know if there's anything else I can help with."
_ASK_FOR_ADDRESS = "What's the best email address to reach you on?"

_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+")

# A sentence is a promise to email the quote when it names email, says "I/we/me" will send it,
# and the thing sent is the quote (or "it/this/that/them"). Anything else (a password-reset link,
# "what's your email?", "our team will reach out on the email you gave") is left alone.
_EMAIL_WORD = re.compile(r"\be-?mail(?:s|ed|ing)?\b|\binbox\b", re.I)
_SEND_VERB = re.compile(r"\b(?:send(?:ing)?|sent|forward(?:ing|ed)?|mail(?:ed|ing)?)\b", re.I)
_EMAIL_AS_VERB = re.compile(
    r"\b(?:i|we)(?:'ll|'ve|'d| will| have| can| could| shall| would)?\s+"
    r"(?:also\s+|gladly\s+|happily\s+)?e-?mail(?:ed)?\b"
    r"|\b(?:should i|shall i|can i|may i|let me|me to|us to|happy to|glad to)\s+e-?mail\b",
    re.I,
)
_FIRST_PERSON = re.compile(
    r"\b(?:i|i'll|i've|i'm|i'd|we|we'll|we've|me|us|let me|should i|shall i|can i|may i)\b", re.I
)
_QUOTE_OBJECT = re.compile(
    r"\b(?:it|this|that|these|them|quotes?|quotation|estimate|proposal|pdf|copy|copies|summary|"
    r"details|breakdown|pricing|price list|information|info|brochure|document|everything)\b",
    re.I,
)
_ASKS_FOR_ADDRESS = re.compile(
    r"\b(?:share|provide|give|drop|enter|type|tell me)\b[^.?!]*\be-?mail\b"
    r"|\bwhat(?:'s| is)\b[^.?!]*\be-?mail\b",
    re.I,
)


def _promises_email(sentence: str) -> bool:
    return bool(
        _EMAIL_WORD.search(sentence)
        and _FIRST_PERSON.search(sentence)
        and _QUOTE_OBJECT.search(sentence)
        and (_SEND_VERB.search(sentence) or _EMAIL_AS_VERB.search(sentence))
    )


def mask_email(address: str) -> str:
    """`alice@example.com` -> `a***@example.com`: enough for the visitor to recognise their own
    address, never enough to repeat someone else's."""
    local, _, domain = address.strip().lower().partition("@")
    return f"{local[:1]}***@{domain}"


def strip_email_promises(reply: str) -> str:
    """Remove every sentence that promises or offers to email the quote.

    A sentence that also asks for the address becomes one plain request for it, so collecting
    the contact still works. Line breaks are kept; a reply that was only a promise is replaced by
    a neutral line rather than left empty.
    """
    kept_lines: list[str] = []
    asked = False
    for line in reply.split("\n"):
        kept: list[str] = []
        for sentence in _SENTENCE_BREAK.split(line):
            if not sentence.strip():
                continue
            if not _promises_email(sentence):
                kept.append(sentence)
            elif _ASKS_FOR_ADDRESS.search(sentence) and not asked:
                kept.append(_ASK_FOR_ADDRESS)
                asked = True
        if kept or not line.strip():
            kept_lines.append(" ".join(kept))
    cleaned = "\n".join(kept_lines).strip()
    return cleaned if cleaned else _FALLBACK


@dataclass(frozen=True)
class SentOutcome:
    """What the platform did about emailing the quote this turn."""

    kind: str  # "queued" | "capped" | "invite"
    masked: str | None = None

    @classmethod
    def queued(cls, masked: str) -> "SentOutcome":
        return cls("queued", masked)

    @classmethod
    def capped(cls) -> "SentOutcome":
        return cls("capped")

    @classmethod
    def invite(cls) -> "SentOutcome":
        return cls("invite")

    @property
    def note(self) -> str:
        if self.kind == "queued":
            return (
                f"I've sent the quote to {self.masked}. "
                "If it doesn't arrive in a few minutes, please check your spam folder."
            )
        if self.kind == "capped":
            return "I couldn't email the quote just now, but it's right here in our chat."
        return "If you'd like a copy by email, share your email address here and I'll send it."


def finalize_reply(reply: str, outcome: SentOutcome | None) -> str:
    """The reply as the visitor sees it: no model-made email promises, plus the true note."""
    cleaned = strip_email_promises(reply)
    if outcome is None:
        return cleaned
    if cleaned == _FALLBACK:
        return outcome.note
    return f"{cleaned}\n\n{outcome.note}"
