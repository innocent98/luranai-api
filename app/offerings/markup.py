"""Price-token markup: what counts as a token, a look-alike, and how little may be removed.

Rules that keep ordinary text safe (review pass 5):

* this module is only ever applied for a tenant that has active catalog items; for everyone else
  the reply path never reaches it;
* a look-alike is bracketed on BOTH sides and holds nothing but `price`, a colon and a number
  (any bracket style, fullwidth forms, invisible characters inside); a bare `price: #5,000`,
  `price: 5000` or `(price: ...` prose is never touched;
* text is matched as written. Folding (NFKC, invisible characters) is used to *detect* a
  look-alike, never to rewrite the surrounding text;
* every quantifier is bounded, so an adversarial message cannot make the scan quadratic.

No imports from the rest of `app.offerings`, so `facts` and `tokens` can both use it.
"""

import html
import re
import unicodedata

_INV = "[­​-‏‪-‮⁠-⁤﻿]{0,3}"
_INVISIBLE = re.compile("[­​-‏‪-‮⁠-⁤﻿]")
_OPEN = "[\\[\\{\\(<【「『《［｛（＜]"
_CLOSE = "[\\]\\}\\)>】」』》］｝）＞]"
_COLON = "[:：]"
_HASH = "[#＃]"
_DIGIT = "[0-9０-９]"


def _letter(ch: str) -> str:
    full = chr(ord(ch) - ord("a") + 0xFF41)
    return f"[{ch}{ch.upper()}{full}{full.upper()}]"


_PRICE = _INV.join(_letter(c) for c in "price")
_SP = r"[ \t]{0,3}"

# `[[price:#12]]`, as the model is told to write it (spacing and case tolerated)
TOKEN = re.compile(rf"\[\[{_SP}price{_SP}:{_SP}#?{_SP}(\d{{1,9}}){_SP}\]\]", re.IGNORECASE)
# the same thing in any bracket style, fullwidth, with invisible characters, closed on both sides
LOOKALIKE = re.compile(
    rf"{_OPEN}{{1,3}}{_INV}{_SP}{_PRICE}{_INV}{_SP}{_COLON}{_SP}{_HASH}?{_SP}{_DIGIT}{{1,9}}{_SP}"
    rf"{_INV}{_CLOSE}{{1,3}}",
    re.IGNORECASE,
)
_GENERIC = re.compile(r"\[\[[^\]\n]{0,200}\]\]|\[\[|\]\]")


def fold(text: str | None) -> str:
    """Width/compat forms folded and invisible characters dropped (for detection only)."""
    return _INVISIBLE.sub("", unicodedata.normalize("NFKC", text or ""))


def _may_hold_price_markup(text: str) -> bool:
    """Cheap gate: the word `price` (however disguised) must be there at all."""
    return "price" in fold(text).lower()


def _unescaped(text: str) -> str:
    """HTML entities read as characters (`&#91;&#91;price:#1&#93;&#93;`), for detection."""
    return html.unescape(text) if "&" in text else text


def has_price_markup(text: str | None) -> bool:
    """An exact token or a bracketed look-alike, valid or not. Bare prose never counts."""
    if not text or not _may_hold_price_markup(_unescaped(text)):
        return False
    seen = _unescaped(text)
    return bool(
        TOKEN.search(text)
        or LOOKALIKE.search(text)
        or LOOKALIKE.search(fold(text))
        or LOOKALIKE.search(fold(seen))
    )


def strip_price_markup(text: str | None) -> str:
    """Exact tokens and bracketed look-alikes removed from the text as written; everything else
    (spaces, line breaks, folding-sensitive characters) is left alone. Byte-identical when there
    is nothing to remove. A look-alike hidden in HTML entities is decoded first (that reply only).
    """
    out = text or ""
    if not _may_hold_price_markup(_unescaped(out)):
        return out
    if not (TOKEN.search(out) or LOOKALIKE.search(out)) and LOOKALIKE.search(fold(_unescaped(out))):
        out = _unescaped(out)
    for _ in range(4):  # removing one can join the halves of another
        cleaned = LOOKALIKE.sub("", TOKEN.sub("", out))
        if cleaned == out:
            break
        out = cleaned
    return out


def strip_brackets(text: str | None) -> str:
    """For item text a tenant wrote (catalog tenants only): also drop `[[...]]` leftovers, and go
    on until stable so removing one cannot join the halves of a token."""
    out = text or ""
    for _ in range(4):
        cleaned = _GENERIC.sub("", strip_price_markup(out))
        if cleaned == out:
            break
        out = cleaned
    return strip_price_markup(out)


def has_generic_brackets(text: str | None) -> bool:
    return bool(text and _GENERIC.search(text))
