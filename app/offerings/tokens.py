"""Price tokens: the model never types an amount for a catalog item, it writes `[[price:#12]]`.

Code resolves each token to that exact item's live `price_text` after the guard and before the
reply is stored, sent or spoken, so a wrong, stale or swapped price cannot come from model text:
the only way to name a price is a reference to a row. Anything token-like that is not a valid
token for an item in the block is a violation, and no token ever leaves the backend.
"""

import re
from collections.abc import Callable, Iterable

from app.offerings import markup
from app.offerings.facts import OfferingFacts
from app.offerings.markup import TOKEN, fold


def token(seq: int) -> str:
    return f"[[price:#{seq}]]"


def token_seqs(text: str) -> list[int]:
    return [int(m.group(1)) for m in TOKEN.finditer(text or "")]


def has_stray_markup(text: str) -> bool:
    """Something token-like left once the exact tokens are removed: a bracketed look-alike
    (`[price:#3]`, `{{price:#3}}`, fullwidth, zero-width) or a stray `[[` / `]]` (catalog replies
    only). Bare prose such as `price: #5,000` is not markup."""
    rest = TOKEN.sub("", text or "")
    return bool(markup.has_price_markup(rest) or markup.has_generic_brackets(fold(rest)))


def has_markup(text: str) -> bool:
    """Anything token-like at all, valid or not."""
    return bool(TOKEN.search(text or "")) or has_stray_markup(text)


def mask(text: str, replacement: str = "PRICE") -> str:
    """Valid tokens replaced by a neutral word, so amount extraction never sees them."""
    return TOKEN.sub(replacement, text or "")


def strip(text: str) -> str:
    """Remove the token text (exact tokens and bracketed look-alikes, and `[[...]]` leftovers) and
    nothing else. Only ever called for a tenant with catalog items."""
    return markup.strip_brackets(text)


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

    out = TOKEN.sub(sub, text or "")
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
