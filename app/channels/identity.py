"""Contact identity: peppered HMAC lookup keys and phone helpers.

`hash_for_lookup` (app.core.encryption) is an unsalted SHA-256: a leaked column of phone
hashes is brute-forceable (10^10 numbers). Every WhatsApp lookup key therefore uses an
HMAC keyed with `WHATSAPP_CONTACT_PEPPER`, with a domain prefix so a phone key can never
collide with a chat-ref key.
"""

from __future__ import annotations

import hashlib
import hmac
import re

from app.core.config import settings

_DIGITS = re.compile(r"^[1-9][0-9]{7,14}$")  # E.164 without '+': 8..15 digits, no leading 0


class PepperMissing(RuntimeError):
    """WHATSAPP_CONTACT_PEPPER is not configured: refuse rather than hash unsalted."""


def _pepper() -> bytes:
    pepper = settings.WHATSAPP_CONTACT_PEPPER
    if not pepper:
        raise PepperMissing("WHATSAPP_CONTACT_PEPPER is not set")
    return pepper.encode()


def _hmac(domain: str, value: str) -> str:
    return hmac.new(_pepper(), f"{domain}:{value}".encode(), hashlib.sha256).hexdigest()


def phone_hmac(digits: str) -> str:
    return _hmac("phone", digits)


def contact_hmac(chat_ref: str) -> str:
    return _hmac("ref", chat_ref)


def normalize_phone(raw: str) -> str | None:
    """Digits with country code, no '+'. Returns None when it is not a plausible number."""
    cleaned = (raw or "").strip().replace(" ", "").replace("-", "")
    if cleaned.startswith("+"):
        return None  # contract: digits only, no '+'
    return cleaned if _DIGITS.fullmatch(cleaned) else None


def mask_phone(digits: str) -> str:
    """`+234•••••6789`: country-code-free tail only, enough for the owner to recognise it."""
    if len(digits) <= 4:
        return "•" * len(digits)
    return f"+{digits[:3]}{'•' * (len(digits) - 7)}{digits[-4:]}"
