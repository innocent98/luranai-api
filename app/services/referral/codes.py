"""Rules for marketer referral codes."""

import re

CODE_MIN_LENGTH = 3
CODE_MAX_LENGTH = 32
RESERVED_CODES = frozenset({"admin", "api", "login", "register", "luran", "support", "www"})

_CODE_PATTERN = re.compile(r"[a-z0-9-]+")


def validate_marketer_code(code: str) -> str:
    """Return the normalised (stripped, lowercase) code, or raise ``ValueError``.

    A code is 3-32 characters of ``[a-z0-9-]`` and must not be a reserved word.
    """
    if not isinstance(code, str):
        raise ValueError("Referral code must be text")
    normalised = code.strip().lower()
    if not CODE_MIN_LENGTH <= len(normalised) <= CODE_MAX_LENGTH:
        raise ValueError(
            f"Referral code must be {CODE_MIN_LENGTH}-{CODE_MAX_LENGTH} characters long"
        )
    if not _CODE_PATTERN.fullmatch(normalised):
        raise ValueError("Referral code may only contain letters, digits and hyphens")
    if normalised in RESERVED_CODES:
        raise ValueError("That referral code is reserved")
    return normalised
