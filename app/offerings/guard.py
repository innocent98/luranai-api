"""The price guard (spec 7.5): code, not prompt, decides whether a reply may be sent.

Structural rules (the model never types a catalog price, see `tokens.py`):

* a price is a token bound to one row (`[[price:#12]]`); an unknown, foreign or malformed token, or a
  token beside the name of a different item, is a violation;
* any literal amount outside a token is a violation, unless it comes from a knowledge-base sentence
  that names no catalog item (a delivery fee), is a plan price in a sentence naming no item, or is a
  pure "You offered ₦X." echo of the customer's own figure;
* discount and agreement wording is a violation when anchored to an item, a price or an offer; when
  the customer is bargaining (or the model returned an offered price) the pipeline sends code-only
  lines (`offer_turn.py`), and this module is the backstop.

This module only judges; the regenerate-then-fall-back flow is `guard_flow.py`.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal

from app.offerings import tokens
from app.offerings.amounts import (
    Amount,
    customer_amounts,
    extract_amounts,
    mask_non_money,
    normalise,
)
from app.offerings.facts import OfferingFacts
from app.offerings.grounding import GroundingResult, singular

_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")
_REF = re.compile(r"#\s?(\d{1,9})\b")
_NAME_STOP = {"out", "and", "the", "for", "with", "new", "set"}
_CUES = re.compile(
    r"\b(?:(?:i|we)(?:'ll|\s+will|\s+can|\s+could|\s+would)\s+"
    r"(?:do|give|offer|accept|sell|take|let|go|make|settle|meet|reduce|lower|drop|knock|cut|agree|throw)"
    r"|deal|agreed?|accept(?:ed)?|meet\s+you|go\s+(?:as\s+)?low|lowest|final\s+price|last\s+price"
    r"|special\s+price|best\s+price|discount(?:ed)?|reduce[ds]?|lower(?:ed)?|dropp?(?:ed|ing)?"
    r"|knock(?:ed|ing)?|slash(?:ed)?|cut|sell\s+(?:it|them|this|that)?\s*(?:to\s+you\s+)?for"
    r"|you\s+can\s+have|take\s+(?:it|them)\s+for|bring(?:s|ing)?\s+(?:it|them|the\s+price)\s+down"
    r"|now\s+(?:only\s+)?(?:at|for)|half"
    r"|works?(?:\s+for\s+(?:me|us))?|fine|okay|ok|sure|alright|agreeable|yours|go\s+ahead|no\s+problem"
    r"|you\s+got\s+it|can\s+do|sounds?\s+(?:good|great|fair))\b",
    re.IGNORECASE,
)


_DISCOUNT_WORDS = re.compile(
    r"half[\s-]?price|\bfor free\b|free of charge|on the house|discount|reduc(?:e|ed|tion)"
    r"|\d[\d,.]*\s*(?:%|percent|per cent)\s*(?:off|discount|reduction)"
    r"|\d[\d,.]*\s*(?:k|naira)?\s+off\b|\b(?:take|knock|knocked|get|extra|some)\s+(?:\w+\s+){0,2}off\b"
    r"|price (?:drop|cut|off)|\boff (?:the )?price\b|markdown|bargain|lower (?:the )?price"
    r"|cut (?:the )?price|get one free|buy one|no (?:extra )?(?:cost|charge)|\bon us\b",
    re.IGNORECASE,
)
_NEGATION = re.compile(
    r"\b(?:can't|cannot|can not|unable to|not able to|won't|will not|don't|do not|never"
    r"|no discounts?|no reductions?|not (?:on )?sale|not discounted)\b",
    re.IGNORECASE,
)
# Only these may stand between a negation and the word it governs ("can't GIVE A discount").
_FILLER = {
    "a", "an", "any", "the", "give", "offer", "do", "get", "you", "ever", "further", "extra",
    "more", "this", "that", "such", "special", "provide", "make", "allow", "apply", "grant",
}  # fmt: skip
_AGREE = re.compile(
    r"\b(?:yes|yeah|yep|okay|ok|sure|deal|agreed?|accept(?:ed)?|alright|perfect|good news|great news"
    r"|granted|approved|confirmed|fine|works?|no problem|of course|certainly|absolutely|done"
    r"|wahala|oya|e don do|it'?s yours|go with that|we can do that|that'?s fair|let'?s go"
    r"|fair enough|you got it|we accept|can do|doable|honou?r|settled|proceed|possible"
    r"|put\s+(?:it|that)\s+through|at\s+(?:that|your)\s+price|we\s+fit|fit\s+do\s+am)\b",
    re.IGNORECASE,
)
_SAME = re.compile(
    r"\b(?:same\s+(?:price|cost|amount)|costs?\s+(?:the\s+)?same|(?:priced|is|are)\s+the\s+same"
    r"|also\s+costs?|for\s+the\s+same|at\s+the\s+same|matches\s+(?:it|that|this))\b",
    re.IGNORECASE,
)
_NOT_PRODUCT = (
    r"(?!\s*(?:sizes?|colou?rs?|options?|photos?|pictures?|variants?|ways?|days?|weeks?|hours?"
    r"|minutes?|mins?|months?|years?|steps?|stores?|branch(?:es)?|locations?|payment|methods?))"
)
_QTY = (
    r"(?<!size\s)(?<!sizes\s)(?<!to\s)(?<!-)(?<![#\d.,₦$£€N])\b(?:[2-9]|[1-9]\d|two|three|four|five|six|seven|eight|nine|ten"
    rf"|dozen)\b(?![,.]\d){_NOT_PRODUCT}"
)
# a sentence that carries a token and speaks of several units or a total prices something the token
# does not: the token is one item's unit price
_TOTAL = re.compile(
    r"\b(?:total|altogether|comes?\s+(?:up\s+)?to|combined|in\s+all|sum|add(?:s|ed|ing)?\s+up)\b",
    re.IGNORECASE,
)
# words that group items under one price; several distinct tokens in a sentence are separate prices
_GROUP = re.compile(
    r"\b(?:pair|set|bundle|both|all\s+(?:of\s+)?our|together|package|the\s+two|those\s+two"
    r"|second|third|every\s+other|everything|every\s+item|all\s+(?:items|products|prices)|any\s+item"
    r"|the\s+whole\s+(?:shop|store|range|menu))\b",
    re.IGNORECASE,
)
_QTY_RE = re.compile(
    rf"{_QTY}|(?<![#\w])x\s?[2-9]\b|\b[2-9]\s?x(?![\w])|\bqty\s*[2-9]\b", re.IGNORECASE
)
# "serves about 10 people", "has 50 pieces", "up to 500 guests": a count that describes the item
_DESCRIBES = re.compile(
    r"\b(?:has|have|contains?|includes?|serves?|seats?|fits?|holds?|about|around|approximately"
    r"|roughly|up\s+to|over|within|under|than|of)\s+(?:about\s+|around\s+)?$",
    re.IGNORECASE,
)


def _bundle_hit(plain: str, distinct_tokens: int) -> bool:
    """A token sentence that quantifies, groups or totals what the token prices."""
    if _TOTAL.search(plain):
        return True
    if distinct_tokens < 2 and _GROUP.search(plain):
        return True
    return any(
        not _DESCRIBES.search(plain[max(0, m.start() - 24) : m.start()])
        for m in _QTY_RE.finditer(plain)
    )


_PER = re.compile(r"\b(?:each|per|apiece|every|a\s+pair|one|unit)\b", re.IGNORECASE)
# a freebie word is only a problem next to a catalog item ("a free Leather Wallet", "the Red Bag as a
# gift"); "feel free", "free size" and "a free dust bag" are ordinary words
_FREE_WORD = re.compile(
    r"\b(?:free(?!\s+(?:size|to|from|of\s+(?!charge)))|complimentary|gift(?:ed)?|freebies?|bonus"
    r"|waive[ds]?|on\s+the\s+house|for\s+nothing)\b",
    re.IGNORECASE,
)
_FREE_PHRASE = re.compile(
    r"\b(?:at\s+no\s+(?:extra\s+)?(?:cost|charge)|half\s+off|buy\s+(?:one|1|two|2)|as\s+(?:a\s+)?(?:gift|bonus|extra)"
    r"|two\s+for|2\s+for|throw(?:n|ing)?\s+in|comes?\s+with\s+(?:it|that|this|them)"
    r"|get\b[^.!?]{0,40}\b(?:too|as\s+well)|price\s+includes|included\s+(?:in|with)\s+the\s+price"
    r"|loyalty|voucher|coupon|cash[\s-]?back|rebate|promo(?:tion|tional)?|on\s+sale|sale\s+price"
    r"|special\s+offer|minus)\b",
    re.IGNORECASE,
)
_FREE_SAME = re.compile(r"\b(?:for\s+the\s+same|at\s+the\s+same)\b", re.IGNORECASE)
_SAME_ALONE = re.compile(r"^\W*(?:the\s+same|same|it\s+matches|matches\s+it)\W*$", re.IGNORECASE)
_PURE_ECHO = re.compile(
    r"^\s*(?:and\s+)?you(?:'ve|\s+have)?\s+(?:offered|said|mentioned|asked\s+for|proposed|"
    r"suggested|quoted|stated)\b",
    re.IGNORECASE,
)


@dataclass
class Violation:
    sentence: str
    reason: str
    amounts: list = field(default_factory=list)


@dataclass
class GuardVerdict:
    ok: bool
    violations: list[Violation] = field(default_factory=list)
    checked: bool = True


def _name_tokens(item: OfferingFacts) -> set[str]:
    return {
        singular(t)
        for t in re.findall(r"[a-z0-9]+", item.name.lower())
        if len(t) >= 3 and t not in _NAME_STOP
    }


def _spans(text: str, needle: str) -> list[tuple[int, int]]:
    return [m.span() for m in re.finditer(re.escape(needle), text)] if needle else []


def named_items(sentence: str, items: list[OfferingFacts]) -> list[OfferingFacts]:
    """The items a sentence mentions (by `#ref`, full name or a name word). A more specific name
    shadows a shorter one it contains: "Black Sneaker Pro" is not also "Black Sneaker"."""
    lowered = sentence.lower()
    words = {singular(w) for w in re.findall(r"[a-z0-9]+", lowered)}
    refs = {int(n) for n in _REF.findall(sentence)}
    hits: list[OfferingFacts] = []
    full_spans: dict[int, list[tuple[int, int]]] = {}
    for item in items:
        spans = _spans(lowered, item.name.lower())
        if spans:
            full_spans[item.seq] = spans
        if item.seq in refs or spans or (_name_tokens(item) & words):
            hits.append(item)
    out = []
    for a in hits:
        a_spans = full_spans.get(a.seq)
        shadowed = bool(a_spans) and all(
            any(
                b.seq != a.seq
                and len(b.name) > len(a.name)
                and a.name.lower() in b.name.lower()
                and any(bs <= s and e <= be for bs, be in full_spans.get(b.seq, []))
                for b in hits
            )
            for s, e in a_spans
        )
        if not shadowed:
            out.append(a)
    return out


def _mentions(
    sentence: str, items: list[OfferingFacts]
) -> list[tuple[int, int, OfferingFacts, int]]:
    """Where a sentence mentions an item: (start, end, item, strength). Strength is how many of the
    item's name words the mention covers, so "the Pro version of the Black Sneaker" is the Pro item,
    not the base one; an explicit #ref is the strongest."""
    low = sentence.lower()
    words = [(m.start(), m.end(), singular(m.group())) for m in re.finditer(r"[a-z0-9]+", low)]
    out = []
    for it in items:
        toks = _name_tokens(it)
        name = it.name.lower()
        for m in re.finditer(re.escape(name), low) if name else []:
            out.append((m.start(), m.end(), it, 2 * max(len(toks), 1) + 1))
        for m in _REF.finditer(sentence):
            if int(m.group(1)) == it.seq:
                out.append((m.start(), m.end(), it, 100))
        hit = [(s0, e0, w) for s0, e0, w in words if w in toks]
        got = {w for _, _, w in hit}
        if toks and got and len(got) >= min(2, len(toks)) or (len(toks) == 1 and got):
            out.append((hit[0][0], max(e0 for _, e0, _ in hit), it, 2 * len(got)))
        elif got:  # one word of a longer name: a weak mention
            out.append((hit[0][0], max(e0 for _, e0, _ in hit), it, 0))
    return out


_PRICE_FOR = re.compile(r"^\s*(?:for|per|on|of)\s+(?:the\s+|a\s+|an\s+|our\s+)?", re.IGNORECASE)


class _Foreign:
    """An active catalog item that is not in this turn's block: a price token can never be its."""

    seq = -1


def _foreign_mentions(sentence: str, names: list[str], known: set[str]) -> list:
    low = sentence.lower()
    words = [(m.start(), m.end(), singular(m.group())) for m in re.finditer(r"[a-z0-9]+", low)]
    out = []
    for name in names:
        key = name.lower()
        if not key or key in known:
            continue
        for m in re.finditer(re.escape(key), low):
            out.append((m.start(), m.end(), _Foreign(), 100))
        toks = {
            singular(t)
            for t in re.findall(r"[a-z0-9]+", key)
            if len(t) >= 3 and t not in _NAME_STOP
        }
        hit = [(s0, e0) for s0, e0, w in words if w in toks]
        if toks and len({w for _, _, w in words if w in toks}) >= min(2, len(toks)):
            out.append((hit[0][0], max(e0 for _, e0 in hit), _Foreign(), 1))
    return out


def bound_items(
    sentence: str, items: list[OfferingFacts], foreign_names: list[str] | None = None
) -> list[tuple[int, set[int] | None]]:
    """For each valid token in the sentence: the items its position refers to (the nearest item
    named before it, or after it when the token is written as "[[price]] for the X"), or None
    when the sentence names no item."""
    hidden = tokens.TOKEN.sub(lambda m: "\x00" * len(m.group(0)), sentence)
    mentions = _mentions(hidden, items)
    if foreign_names:
        mentions += _foreign_mentions(hidden, foreign_names, {i.name.lower() for i in items})
    result: list[tuple[int, set[int] | None]] = []
    for m in tokens.TOKEN.finditer(sentence):
        seq = int(m.group(1))
        before = [x for x in mentions if x[1] <= m.start()]
        after = [x for x in mentions if x[0] >= m.end()]
        pool = None
        forward = _PRICE_FOR.match(sentence[m.end() :]) if after else None
        right_after = [x for x in after if forward and x[0] == m.end() + forward.end()]
        if forward and after and not right_after:
            result.append((seq, {-2}))  # "for the cheap Red bag": cannot tell whose, fail closed
            continue
        if right_after:
            pool = right_after
        elif before:
            nearest = max(x[1] for x in before)
            pool = [x for x in before if x[1] == nearest]
        elif after:
            nearest = min(x[0] for x in after)
            pool = [x for x in after if x[0] == nearest]
        if not pool:
            result.append((seq, None))
            continue
        top = max(x[3] for x in pool)
        result.append((seq, {x[2].seq for x in pool if x[3] == top}))
    return result


def _clause_anchored(clause: str, items: list[OfferingFacts], names: list[str]) -> bool:
    """Does this clause name a catalog item, a token (masked as PRICE) or a #ref?"""
    return bool(
        "PRICE" in clause or named_items(clause, items) or _names_catalog_strongly(clause, names)
    )


def _names_strongly(sentence: str, items: list[OfferingFacts]) -> bool:
    """A ref, a full name, or at least two of an item's name words ("Pro plan" is not an item)."""
    lowered = sentence.lower()
    words = {singular(w) for w in re.findall(r"[a-z0-9]+", lowered)}
    refs = {int(n) for n in _REF.findall(sentence)}
    for i in items:
        toks = _name_tokens(i)
        if i.seq in refs or (i.name and i.name.lower() in lowered):
            return True
        if toks and len(toks & words) >= min(2, len(toks)):
            return True
    return False


def _names_catalog_strongly(sentence: str, names: list[str]) -> bool:
    """A full item name, or two of its name words, from the tenant's whole active catalog."""
    lowered = sentence.lower()
    words = {singular(w) for w in re.findall(r"[a-z0-9]+", lowered)}
    for name in names:
        toks = {
            singular(t)
            for t in re.findall(r"[a-z0-9]+", name.lower())
            if len(t) >= 3 and t not in _NAME_STOP
        }
        if name and name.lower() in lowered:
            return True
        if toks and len(toks & words) >= min(2, len(toks)):
            return True
    return False


_BREAK = re.compile(r"[.!?;,:\n]")


def _without_names(text: str, names: list[str]) -> str:
    """Item names blanked out: "Catering Package" and "Party Pair" are not groupings."""
    for name in sorted({n for n in names if n}, key=len, reverse=True):
        text = re.sub(re.escape(name), " " * len(name), text, flags=re.IGNORECASE)
    return text


def _free_beside_item(sentence: str, names: list[str]) -> bool:
    """A freebie word right beside a catalog item's full name or #ref ("a free Leather Wallet",
    "the Red Bag as a gift"): at most two words between, no clause break, not negated."""
    low = sentence.lower()
    spots = [m.span() for n in names if n for m in re.finditer(re.escape(n.lower()), low)]
    spots += [m.span() for m in _REF.finditer(sentence)]
    for m in _FREE_WORD.finditer(sentence):
        if _negated(sentence, m):
            continue
        for s0, e0 in spots:
            gap = sentence[m.end() : s0] if s0 >= m.end() else sentence[e0 : m.start()]
            if (s0 >= m.end() or e0 <= m.start()) and not _BREAK.search(gap):
                if len(gap.split()) <= (2 if s0 >= m.end() else 1):
                    return True
    return False


_CLAUSE = re.compile(
    r"(?<!\d),(?!\d)|;|\b(?:and|while|but|plus|whereas|also)\b",
    re.IGNORECASE,
)


def _clause_at(text: str, pos: int) -> str:
    """The clause of a sentence an offset falls in ("delivery within Lagos is ₦2,000" in "The
    sneaker is PRICE and delivery within Lagos is ₦2,000")."""
    start = 0
    for m in _CLAUSE.finditer(text):
        if m.end() <= pos:
            start = m.end()
        elif m.start() >= pos:
            return text[start : m.start()]
    return text[start:]


def _names_any(sentence: str, names: list[str]) -> bool:
    """Does a sentence name a catalog item (any of the tenant's active items, in the block or not)?"""
    words = {singular(w) for w in re.findall(r"[a-z0-9]+", sentence.lower())}
    for name in names:
        toks = {
            singular(t)
            for t in re.findall(r"[a-z0-9]+", name.lower())
            if len(t) >= 3 and t not in _NAME_STOP
        }
        if toks and toks & words:
            return True
    return False


def _negated(sentence: str, match: re.Match) -> bool:
    """A negation directly governs the word: only filler words between, no clause break."""
    lo = max(0, match.start() - 45)
    window = sentence[lo : match.end()]
    hits = list(_NEGATION.finditer(window))
    if not hits:
        return False
    word_start = match.start() - lo
    last = hits[-1]
    if last.end() > word_start:  # the negation phrase is the word ("no discount")
        return True
    between = window[last.end() : word_start]
    if re.search(r"[,;:?!\u2014\u2013]|\bbut\b|\bhowever\b", between):
        return False
    words = re.findall(r"[a-z']+", between.lower())
    return len(words) <= 4 and all(w in _FILLER for w in words)


def _unnegated(pattern: re.Pattern, sentence: str) -> bool:
    return any(not _negated(sentence, m) for m in pattern.finditer(sentence))


def _values(texts: Iterable[str], small_ok: bool = False) -> set[Decimal]:
    """The money figures in the customer's messages, read as anchored (a catalog item is in play)."""
    return {a.value for t in texts for a in customer_amounts(t, small_ok=small_ok, strict=True)}


def _has(values: Iterable, v: Decimal) -> bool:
    return any(v == x for x in values)


_GENERIC = {
    "price", "prices", "cost", "costs", "pay", "paid", "only", "from", "this", "that", "with", "will",
    "your", "have", "order", "orders", "naira", "each", "just", "also", "than", "then", "they", "them",
    "which", "what", "when", "where", "about", "above", "below", "under", "over", "starts", "start",
    "starting", "total", "item", "items",
}  # fmt: skip


def _content_words(text: str) -> set[str]:
    return {
        singular(w)
        for w in re.findall(r"[a-z]{4,}", text.lower())
        if singular(w) not in _GENERIC and w not in _GENERIC
    }


def _kb_values(kb_text: str, names: list[str], small_ok: bool) -> dict[Decimal, list[set[str]]]:
    """Amounts from KB sentences that name no catalog item (a stale KB price must not win), each
    with the content words of the sentence it came from: a reply may use that amount only in a
    sentence about the same thing ("delivery outside Lagos"), not as a price for something else."""
    out: dict[Decimal, list[set[str]]] = {}
    for sentence in _SENTENCE.split(normalise(kb_text)):
        if _names_any(sentence, names):
            continue
        words = _content_words(sentence)
        for a in extract_amounts(mask_non_money(sentence, names), small_ok=small_ok):
            if a.kind == "money":
                out.setdefault(a.value, []).append(words)
    return out


def _kb_allows(kb_money: dict, value: Decimal, sentence: str) -> bool:
    mine = _content_words(sentence)
    return any(v == value and any(mine & w for w in sets) for v, sets in kb_money.items())


def _pure_echo(sentence: str, amount: Amount) -> bool:
    """ "You offered ₦9,000.": the customer's figure quoted back, nothing said about it."""
    after = sentence[amount.end :]
    return (
        bool(_PURE_ECHO.match(sentence))
        and not re.search(r"[,;:]|\b(?:so|and|but|which|that|then|because)\b", after)
        and len(after.split()) <= 3
    )


def check_reply(
    reply: str,
    grounding: GroundingResult,
    *,
    customer_texts: Iterable[str] = (),
    kb_text: str = "",
    tier_amounts: Iterable[int] = (),
    offer_in_play: bool = False,
) -> GuardVerdict:
    customer_texts = list(customer_texts)
    items = grounding.items
    injected = {i.seq for i in items}
    all_prices = {Decimal(i.price) for i in items if i.price is not None}
    small_ok = any(p < 1000 for p in all_prices)
    catalog_names = list({*grounding.catalog_names, *(i.name for i in items)})
    customer_money = _values(customer_texts, small_ok)
    kb_money = _kb_values(kb_text, catalog_names, small_ok)
    tiers = {Decimal(t) for t in tier_amounts}
    currency = (grounding.currency or "NGN").upper()
    matched_seqs = set(grounding.matched_seqs)
    matched = {Decimal(i.price) for i in items if i.seq in matched_seqs and i.price}
    ceiling = max(matched or all_prices, default=None)
    recent = customer_texts[-3:]  # a figure named a turn or two ago still frames this reply
    offer = offer_in_play or (
        ceiling is not None and any(v < ceiling for v in _values(recent, small_ok))
    )

    # a percentage may be repeated only when a source already holds it (a KB sentence such as VAT,
    # an item description such as "100% cotton", or the customer's own words)
    percents = {
        a.value
        for src in (kb_text, grounding.block, *customer_texts)
        for a in extract_amounts(src or "")
        if a.kind == "percent"
    }
    violations: list[Violation] = []
    text = normalise(reply)
    if grounding.strict:
        if tokens.token_seqs(text) or tokens.has_stray_markup(text):
            violations.append(Violation(reply, "strict"))
    elif tokens.has_stray_markup(text):
        violations.append(Violation(reply, "bad_token"))

    sentences = [s.strip() for s in _SENTENCE.split(text) if s.strip()]
    # a sentence that quotes the customer's own figure back poisons agreement words anywhere
    for sentence in sentences:
        for a in extract_amounts(
            mask_non_money(tokens.mask(sentence), catalog_names), small_ok=small_ok
        ):
            if a.kind == "money" and _has(customer_money, a.value) and _pure_echo(sentence, a):
                offer = True
    carried: set[int] = set()  # the items the last sentence naming any was about
    for sentence in sentences:
        seqs = tokens.token_seqs(sentence)
        plain = tokens.mask(sentence)  # the "#1" inside a token is not a mention of item #1
        named = named_items(plain, items)
        names_catalog = _names_catalog_strongly(plain, catalog_names)
        anchored = bool(named or seqs or names_catalog)
        masked = mask_non_money(plain, catalog_names)
        amounts = extract_amounts(masked, small_ok=small_ok, anchored=anchored)
        named_seqs = {i.seq for i in named}
        if not grounding.strict:
            bound = {seq: pool for seq, pool in bound_items(sentence, items, catalog_names)}
            for seq in seqs:
                pool = bound.get(seq)
                if seq not in injected:
                    violations.append(Violation(sentence, "unknown_token"))
                elif pool is not None and seq not in pool:
                    violations.append(Violation(sentence, "token_mismatch"))  # beside another item
                elif pool is None and names_catalog:
                    violations.append(
                        Violation(sentence, "token_mismatch")
                    )  # names an item outside the block
                elif pool is None and carried:
                    if seq not in carried:  # "It costs ..." after another item
                        violations.append(Violation(sentence, "token_mismatch"))
                elif pool is None and matched_seqs != {seq}:
                    # nothing in the sentence says whose price this is: only the one item the
                    # customer's question matched can be meant
                    violations.append(Violation(sentence, "token_mismatch"))
            if seqs and named_seqs:
                last = bound.get(seqs[-1])
                carried = last if last else named_seqs
            elif named_seqs:
                carried = named_seqs
            if (
                seqs
                and _bundle_hit(_without_names(plain, catalog_names), len(set(seqs)))
                and not _PER.search(plain)
            ):
                violations.append(Violation(sentence, "bundle_price"))
            if _unnegated(_FREE_PHRASE, sentence) or (
                (anchored or seqs)
                and (_unnegated(_FREE_SAME, sentence) or _free_beside_item(sentence, catalog_names))
            ):
                violations.append(Violation(sentence, "freebie_wording"))
            if _SAME_ALONE.match(sentence):
                violations.append(Violation(sentence, "same_price_claim"))
            if (seqs or named) and _SAME.search(plain):
                violations.append(Violation(sentence, "same_price_claim"))
            if len(set(seqs)) == 1 and len(named_seqs) > 1 and re.search(r"\bboth\b", plain, re.I):
                violations.append(Violation(sentence, "same_price_claim"))
            if (anchored or amounts or offer or _names_any(plain, catalog_names)) and _unnegated(
                _DISCOUNT_WORDS, sentence
            ):
                violations.append(Violation(sentence, "discount_wording"))
            if offer and (_unnegated(_AGREE, sentence) or _unnegated(_CUES, sentence)):
                violations.append(Violation(sentence, "agreement_after_offer"))
        for a in amounts:
            if a.kind == "percent":
                if not grounding.strict and a.value not in percents:
                    violations.append(Violation(sentence, "percent_claim", [a.value]))
                continue  # a percentage is a business term nobody sanctioned unless it is known
            if a.kind != "money":
                continue
            reason = None
            if grounding.strict:
                reason = "strict"
            elif a.currency and a.currency != currency:
                reason = "currency"
            elif (
                _kb_allows(kb_money, a.value, _clause_at(plain, a.start))
                and not _clause_anchored(_clause_at(plain, a.start), items, catalog_names)
                and not (offer and _has(customer_money, a.value))
            ) or (_has(tiers, a.value) and not (_names_strongly(plain, items) or names_catalog)):
                reason = None
            elif _has(customer_money, a.value) and _pure_echo(sentence, a):
                reason = None
            else:
                reason = "literal_amount"
            if reason:
                violations.append(Violation(sentence, reason, [a.value]))
    return GuardVerdict(ok=not violations, violations=violations)
