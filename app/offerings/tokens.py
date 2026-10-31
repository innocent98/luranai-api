"""Price tokens: the model never types an amount for a catalog item, it writes `[[price:#12]]`.

Code resolves each token to that exact item's live `price_text` after the guard and before the
reply is stored, sent or spoken, so a wrong, stale or swapped price cannot come from model text:
the only way to name a price is a reference to a row. Anything token-like that is not a valid
token for an item in the block is a violation, and no token ever leaves the backend.
"""

import re
import unicodedata
from collections.abc import Callable, Iterable

from app.offerings.facts import OfferingFacts

TOKEN = re.compile(r"\[\[\s*price\s*:\s*#?(\d{1,9})\s*\]\]", re.IGNORECASE)
_BRACKETS = re.compile(r"\[\[|\]\]")
_INVISIBLE = re.compile("[\u00ad\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff]")
# a token-like leftover in any bracket style: `price:#3` anywhere, `price: 3` only inside a bracket
_LOOSE = re.compile(r"price\s*[:：]\s*#|[\[\{<(【「『]\s*price\s*[:：]\s*\d", re.IGNORECASE)
_TOKEN_SPAN = re.compile(
    r"[\[\{<(【「『]{0,3}\s*price\s*[:：]\s*#?\s*\d{0,9}\s*[\]\}>)】」』]{0,3}|\[\[[^\]\n]*\]\]|\[\[|\]\]",
    re.IGNORECASE,
)


def fold(text: str | None) -> str:
    """Width/compat forms folded and invisible characters dropped: what the guard reads."""
    return _INVISIBLE.sub("", unicodedata.normalize("NFKC", text or ""))


def token(seq: int) -> str:
    return f"[[price:#{seq}]]"


def token_seqs(text: str) -> list[int]:
    return [int(m.group(1)) for m in TOKEN.finditer(text or "")]


def has_stray_markup(text: str) -> bool:
    """`[[`, `]]` or a `price:#N` look-alike (any bracket style) left once the valid tokens are
    removed: a malformed or foreign token. Reads the folded text, like the guard."""
    rest = TOKEN.sub("", fold(text))
    return bool(_BRACKETS.search(rest) or _LOOSE.search(rest))


def has_markup(text: str) -> bool:
    """Anything token-like at all, valid or not."""
    return bool(TOKEN.search(fold(text))) or has_stray_markup(text)


def mask(text: str, replacement: str = "PRICE") -> str:
    """Valid tokens replaced by a neutral word, so amount extraction never sees them."""
    return TOKEN.sub(replacement, text or "")


def strip(text: str) -> str:
    """Remove the token text and nothing else: spaces and line breaks around it stay as written
    (no catalog block was injected, so this must never reshape an ordinary reply)."""
    return _TOKEN_SPAN.sub("", fold(text))


def resolve(text: str, price_text: Callable[[int], str | None]) -> str | None:
    """Replace every token by the live price text; None if any token cannot be resolved."""
    failed = False

    def sub(m: re.Match) -> str:
        nonlocal failed
        value = price_text(int(m.group(1)))
        if value is None:
            failed = True
            return ""
        return value

    out = TOKEN.sub(sub, fold(text))
    if failed or has_stray_markup(out):
        return None
    return out


def price_order_line(items: Iterable[OfferingFacts]) -> str:
    """ "Price order, lowest first: #3, #1, #2": lets the model compare without ever seeing amounts."""
    priced = sorted((i for i in items if i.price is not None), key=lambda i: (i.price, i.seq))
    if len(priced) < 2:
        return ""
    refs = ", ".join(i.ref for i in priced)
    tail = " (prices on request last)" if any(i.price is None for i in items) else ""
    return f"Price order, lowest first: {refs}{tail}."
