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

from app.db.models.offering import OfferingAvailability
from app.offerings import tokens
from app.offerings import lexicon
from app.offerings.bargain import cap, fold, has_bargain_wording
from app.offerings.amounts import (
    Amount,
    blank_names,
    find_all,
    customer_amounts,
    extract_amounts,
    mask_non_money,
    normalise,
)
from app.offerings.facts import OfferingFacts
from app.offerings.grounding import GroundingResult
from app.offerings.names import (
    NAME_STOP,
    NameIndex,
    as_index,
    lower_same_len,
    singular,
    words_of,
)

_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")
_REF = re.compile(r"#\s?(\d{1,9})\b")
_NAME_STOP = NAME_STOP
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
    r"half[\s-]?price|discount|reduc(?:e|ed|tion)"
    r"|\d[\d,.]*\s*(?:%|percent|per cent)\s*(?:off|discount|reduction)"
    r"|\d[\d,.]*\s*(?:k|naira)?\s+off\b|\b(?:take|knock|knocked|get|extra|some)\s+(?:\w+\s+){0,2}off\b"
    r"|price (?:drop|cut|off)|\boff (?:the )?price\b|markdown|bargain|lower (?:the )?price"
    r"|cut (?:the )?price|get one free|buy one|no (?:extra )?(?:cost|charge)|\bon us\b"
    r"|(?:go|come|can|could)\s+(?:a\s+(?:bit|little)\s+)?(?:lower|down)\b|for\s+less\b"
    r"|below\s+(?:the\s+)?(?:listed|list|price)|(?:a\s+(?:little|bit)|slightly)\s+(?:off|below|less|lower)"
    r"|better\s+deal|good\s+price|the\s+least\s+we\s+can\s+do|let\s+it\s+go\s+for|something\s+small"
    r"|manage\s+something|special\s+rate|sort\s+you\s+out|something\s+nice|take\s+care\s+of\s+you"
    r"|(?:for|at)\s+that\s+(?:price|amount|figure)|you\s+can\s+have\s+(?:both|them|it|these)\s+for",
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
_AGREE_STRONG = re.compile(
    r"\b(?:deal|agreed?|granted|approved|we\s+can\s+do\s+that|can\s+do\s+that|honou?r(?:ed)?"
    r"|(?:we\s+)?accept(?:ed)?(?!\s+(?:bank|cash|card|transfers?|payments?|pos|visa|master\s?card|cheques?"
    r"|crypto|ussd|naira|dollars?|orders?|returns?|exchanges?))"
    r"|it'?s\s+yours|you\s+got\s+it|settled|put\s+(?:it|that)\s+through|at\s+(?:that|your)\s+price"
    r"|go\s+with\s+that|let'?s\s+go|fair\s+enough|that'?s\s+fair|e\s+don\s+do|we\s+fit"
    r"|fit\s+do\s+am|wahala|oya|doable|we\s+agree|i\s+agree)\b",
    re.IGNORECASE,
)
# ordinary yes/sure/okay: an agreement only when the sentence is about an item, a price or an offer
_AGREE_WEAK = re.compile(
    r"\b(?:yes|yeah|yep|okay|ok|sure|alright|perfect|confirmed|fine|works?|no\s+problem|of\s+course"
    r"|certainly|absolutely|done|possible|proceed|good\s+news|great\s+news)\b",
    re.IGNORECASE,
)
_PRICE_TALK = re.compile(
    r"\b(?:price|prices|priced|offer|offered|amount|figure|budget|bid|proposal|cost|costs|pay|payment"
    r"|naira|total|discount|your\s+(?:number|request)|for\s+that|have\s+it|owner\s+(?:said|agreed)"
    r"|that\s+(?:price|amount|figure|offer)|sorted|you\s+win)\b",
    re.IGNORECASE,
)
_SAME = re.compile(
    r"\b(?:same\s+(?:price|cost|amount)|costs?\s+(?:the\s+)?same|(?:priced|is|are)\s+the\s+same"
    r"|also\s+costs?|for\s+the\s+same|at\s+the\s+same|matches\s+(?:it|that|this))\b",
    re.IGNORECASE,
)
# a count before one of these is a unit of measure or a time span, never a bundle of the item
_UNITLIKE = (
    r"(?:sizes?|colou?rs?|options?|photos?|pictures?|variants?|ways?|days?|weeks?|hours?"
    r"|minutes?|mins?|months?|years?|steps?|stores?|branch(?:es)?|locations?|payment|methods?"
    r"|inch(?:es)?|cm|mm|sqm|gb|tb|mp|hz|ghz|mah|watts?|volts?|kg|litres?|liters?)"
)
_NOT_PRODUCT = (
    rf"(?!\s*{_UNITLIKE}\b)(?!\s{{0,3}}(?:to|-|\u2013)\s{{0,3}}\d{{1,3}}\s{{0,3}}{_UNITLIKE}\b)"
)
# nouns that describe a feature of what is sold ("2 SIM slots", "parking for 2 cars"): not a bundle
# unless the shop sells that noun (then "2 rooms at [[price]]" is a real quantity)
_FEATURE_NOUNS = {
    "sim", "slot", "camera", "lens", "port", "hdmi", "usb", "speaker", "core", "screen", "toilet",
    "bathroom", "bedroom", "bed", "car", "space", "floor", "door", "window", "seat", "room",
    "people", "person", "guest", "pax", "parking", "garage", "balcony", "kitchen", "bay", "side", "pocket",
    "layer", "star",
}  # fmt: skip
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
# a percentage is a business term only beside money talk; "100% original", "charges to 50%" are specs
_FIN_PERCENT = re.compile(
    r"\b(?:deposit|fees?|commission|interest|tax|vat|payment|instal+ments?|down\s*payment|upfront"
    r"|advance|balance|refund|discount|off|reduction|less|mark-?up|surcharge|price|cost|pay|paid"
    r"|rate|extra|more|save|savings?|cheaper|cash[\s-]?back|promo\w*|sale|bonus|below|lower|slash\w*"
    r"|special|waive[ds]?|knock\w*)\b",
    re.IGNORECASE,
)
# properties a percentage can describe: "100% original", "charges to 50%", "battery health 90%"
_SPEC_PERCENT = re.compile(
    r"\b(?:original|genuine|authentic|pure|cotton|polyester|leather|wool|silk|nylon|linen|fabric"
    r"|material|battery|charge[sd]?|charging|health|waterproof|humidity|protection|alcohol|sugar"
    r"|fat|organic|natural|recycled|cacao|cocoa|spf|uv|brightness|volume|capacity|power)\b",
    re.IGNORECASE,
)
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


def _feature_count(plain: str, end: int, sold: set[str]) -> bool:
    """The count ending at `end` describes a feature ("5 toilets", "2 to 3 cars"), not units sold."""
    rest = plain[end:]
    rng = re.match(r"\s{0,3}(?:to|-|\u2013)\s{0,3}\d{1,3}", rest, re.IGNORECASE)
    if rng:
        rest = rest[rng.end() :]
    nxt = re.match(r"\s{0,3}([a-z]+)", rest, re.IGNORECASE)
    word = singular(nxt.group(1).lower()) if nxt else ""
    return word in _FEATURE_NOUNS and word not in sold


def _bundle_hit(plain: str, distinct_tokens: int, sold: set[str] = frozenset()) -> bool:
    """A token sentence that quantifies, groups or totals what the token prices. `sold`: the words
    of the shop's item names (a feature noun the shop sells is a real quantity)."""
    if _TOTAL.search(plain):
        return True
    if distinct_tokens < 2 and _GROUP.search(plain):
        return True
    return any(
        not _DESCRIBES.search(plain[max(0, m.start() - 24) : m.start()])
        and not _feature_count(plain, m.end(), sold)
        and not _model_number(plain, m, sold)
        for m in _QTY_RE.finditer(plain)
    )


def _sold_words(names) -> set[str]:
    """Words of the shop's item names, plus the adjacent "word number" pairs ("iphone 14")."""
    index = as_index(names)
    cached = index.__dict__.get("_sold")
    if cached is not None:
        return cached
    out: set[str] = set()
    for n in index.names:
        toks = re.findall(r"[a-z0-9]+", n.lower())
        for w in toks:
            if len(w) >= 3 or w.isdigit():
                out.add(singular(w))
        out.update(
            f"{singular(a)} {b}" for a, b in zip(toks, toks[1:], strict=False) if b.isdigit()
        )
    index.__dict__["_sold"] = out
    return out


def _model_number(plain: str, m: re.Match, sold: set[str]) -> bool:
    """ "iPhone 14": the word before the number and the number together form a pair that occurs in
    a catalog name ("iphone 14"), so it is part of the name, not a quantity."""
    prev = re.search(r"([a-z]+)\s{0,2}$", plain[: m.start()].lower())
    return bool(prev) and f"{singular(prev.group(1))} {m.group(0)}" in sold


_AVAILABLE = re.compile(
    r"\b(?:available|in\s+stock|ready|we\s+have\s+(?:it|them|this)|can\s+order|order\s+now"
    r"|ships?\s+today|get\s+it\s+now)\b",
    re.IGNORECASE,
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
_FREE_SAME = re.compile(
    r"\b(?:for\s+the\s+same|at\s+the\s+same|for\s+free|free\s+of\s+charge|on\s+the\s+house)\b",
    re.IGNORECASE,
)
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
        if (len(t) >= 3 or t.isdigit()) and t not in _NAME_STOP
    }


def _spans(text: str, needle: str) -> list[tuple[int, int]]:
    return [(i, i + len(needle)) for i in find_all(text, needle)] if needle else []


def named_items(sentence: str, items: list[OfferingFacts]) -> list[OfferingFacts]:
    """The items a sentence mentions (by `#ref`, full name or a name word). A more specific name
    shadows a shorter one it contains: "Black Sneaker Pro" is not also "Black Sneaker"."""
    lowered = lower_same_len(sentence)
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
    low = lower_same_len(sentence)
    words = [(m.start(), m.end(), singular(m.group())) for m in re.finditer(r"[a-z0-9]+", low)]
    out = []
    for it in items:
        toks = _name_tokens(it)
        name = it.name.lower()
        for i in find_all(low, name) if name else []:
            out.append((i, i + len(name), it, 2 * max(len(toks), 1) + 1))
        for m in _REF.finditer(sentence):
            if int(m.group(1)) == it.seq:
                out.append((m.start(), m.end(), it, 100))
        hits = [(s0, e0, w) for s0, e0, w in words if w in toks]
        for cluster in _clusters(hits):  # one mention = name words written close together
            got = {w for _, _, w in cluster}
            span = (cluster[0][0], cluster[-1][1])
            if len(got) >= min(2, len(toks)) or (len(toks) == 1 and got):
                out.append((*span, it, 2 * len(got)))
            elif not (
                _SPEC_TOKEN.match(cluster[0][2]) or cluster[0][2].isdigit()
            ):  # one word of a longer name: weak
                out.append((*span, it, 0))
    return out


def _top_mentions(sentence: str, items: list[OfferingFacts]) -> set[int]:
    """The items a sentence is most specifically about (its strongest mentions): what "It costs
    ..." in the next sentence can refer to. Two equally strong candidates stay ambiguous."""
    hidden = tokens.TOKEN.sub(lambda m: "\x00" * len(m.group(0)), sentence)
    found = _mentions(hidden, items)
    if not found:
        return set()
    top = max(m[3] for m in found)
    return {m[2].seq for m in found if m[3] == top}


_SPEC_TOKEN = re.compile(r"^\d+(?:gb|tb|mb|mp|hz|ghz|mah|inch|cm|mm|kg|w|v|k|g)$")


def _clusters(hits: list, gap: int = 20) -> list[list]:
    """Name-word hits grouped by proximity: "Red Bag" twice in a sentence is two mentions."""
    groups: list[list] = []
    for h in hits:
        if groups and h[0] - groups[-1][-1][1] <= gap:
            groups[-1].append(h)
        else:
            groups.append([h])
    return groups


_PRICE_FOR = re.compile(r"^\s*(?:for|per|on|of)\s+(?:the\s+|a\s+|an\s+|our\s+)?", re.IGNORECASE)


class _Foreign:
    """An active catalog item that is not in this turn's block: a price token can never be its."""

    seq = -1


def _foreign_mentions(sentence: str, names, known: set[str]) -> list:
    """Mentions of catalog items that are NOT in this turn's block (a token can never be theirs).

    Cost follows the sentence, not the catalog: a name is looked at only when it shares words with
    the sentence, the word positions are computed once, and equal spans from different names (a
    catalog full of "... Pro" items) collapse into one mention."""
    index = as_index(names)
    cache = index.__dict__.setdefault("_foreign", {})
    cache_key = (sentence, frozenset(known))
    if cache_key in cache:  # a reply (or its regeneration) often repeats a sentence
        return cache[cache_key]
    low = lower_same_len(sentence)
    positions: dict[str, list[tuple[int, int]]] = {}
    for m in re.finditer(r"[a-z0-9]+", low):
        positions.setdefault(singular(m.group()), []).append((m.start(), m.end()))
    word_set = set(positions)
    spans: set[tuple[int, int, int]] = set()
    for n in index.candidates(word_set):
        key = index.low[n]
        if key in known:
            continue
        toks = index.tokens[n]
        matched = toks & word_set
        if toks and not matched:
            continue
        if toks <= word_set:  # every word is there: the full name may be too
            for i in find_all(low, key):
                spans.add((i, i + len(key), 100))
        if toks and len(matched) >= min(2, len(toks)):
            hit = [pos for w in matched for pos in positions[w]]
            spans.add((min(a for a, _b in hit), max(b for _a, b in hit), 1))
    result = [(a, b, _Foreign(), strength) for a, b, strength in spans]
    if len(cache) < 512:
        cache[cache_key] = result
    return result


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


def _names_catalog_strongly(sentence: str, names) -> bool:
    """A full item name, or two of its name words, from the tenant's whole active catalog."""
    index = as_index(names)
    lowered = sentence.lower()
    words = words_of(sentence)
    for i in index.candidates(words):
        if index.low[i] in lowered:
            return True
        toks = index.tokens[i]
        if toks and len(toks & words) >= min(2, len(toks)):
            return True
    return False


_BREAK = re.compile(r"[.!?;,:\n]")


_PAIR_FOR = re.compile(r"\b(?:two|2)\s+for\b", re.IGNORECASE)


_TOKEN_EACH = re.compile(
    r"\bPRICE\s{0,2}(?:each|apiece|per\s+\w+)|\b(?:each|apiece)\s+(?:is|costs?|at|for)\s+PRICE",
    re.IGNORECASE,
)
_PRICE_OF_ONE = re.compile(
    r"\bprice\s+of\s+(?:one|a|1)\b|\bfor\s+the\s+price\s+of\b", re.IGNORECASE
)


def _without_per_pair(sentence: str, plain: str, seqs: list[int]) -> str:
    """ "You can get 2 for your staff; each is [[price]]": 'each' beside a valid token means the
    token is the unit price, so the "2 for" wording is a quantity, not a bundle offer."""
    if seqs and _TOKEN_EACH.search(plain) and not _PRICE_OF_ONE.search(plain):
        return _PAIR_FOR.sub(" ", sentence)
    return sentence


def _without_names(text: str, names: list[str]) -> str:
    """Item names blanked out: "Catering Package" and "Party Pair" are not groupings."""
    return blank_names(text, names)


def _free_same_hit(sentence: str, plain: str, items, names: list[str]) -> bool:
    """ "for free" / "for the same" in a clause that is about an item ("we can write a name on it for
    free" is about the writing, not the cake)."""
    for m in _FREE_SAME.finditer(sentence):
        if _negated(sentence, m):
            continue
        clause = _clause_at(plain, m.start())
        if (
            "PRICE" in clause
            or named_items(clause, items)
            or _names_catalog_strongly(clause, names)
            or _REF.search(clause)
        ):
            return True
    return False


_NUMBERS = re.compile(r"\d[\d,.]*")


def _says_sold_out_item_is_available(sentence: str, plain: str, items) -> bool:
    """ "Yes, the White sneaker is available in size 42" when the block marks it sold out: judged
    per clause, so "the Corolla is sold out; the Camry is available" is fine."""
    for m in _AVAILABLE.finditer(sentence):
        if _negated(sentence, m):
            continue
        top = _top_mentions(_clause_at(plain, m.start()), items)
        if any(i.seq in top and i.availability == OfferingAvailability.OUT_OF_STOCK for i in items):
            return True
    return False


def _kb_states(kb_sentences: list[tuple[set[str], set[str]]], sentence: str) -> bool:
    """The sentence is (nearly) a KB sentence that itself holds a discount word: the tenant stated
    that offer and the model may relay it. Close means: at least 70% of its content words are the
    KB sentence's, and every number it states is the KB sentence's."""
    mine = _content_words(sentence)
    numbers = set(_NUMBERS.findall(sentence))
    return any(
        len(mine & words) >= max(3, int(0.7 * len(mine))) and numbers <= kb_numbers
        for words, kb_numbers in kb_sentences
    )


def _kb_discount_sentences(kb_text: str) -> list[tuple[set[str], set[str]]]:
    return [
        (_content_words(sent), set(_NUMBERS.findall(sent)))
        for sent in _SENTENCE.split(normalise(kb_text or ""))
        if _DISCOUNT_WORDS.search(sent)
    ]


def _free_beside_item(
    sentence: str, names: list[str], items: list[OfferingFacts] | None = None
) -> bool:
    """A freebie word right beside a catalog item's full name or #ref ("a free Leather Wallet",
    "the Red Bag as a gift"): at most two words between, no clause break, not negated."""
    spots = [(a, b) for a, b, _i in as_index(names).mentions(sentence)]
    spots += [m.span() for m in _REF.finditer(sentence)]
    # a partial name ("black sneaker" for "Canvas sneaker black") still points at the item
    spots += [(a, b) for a, b, _it, strength in _mentions(sentence, items or []) if strength >= 2]
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


def _names_any(sentence: str, names) -> bool:
    """Does a sentence name a catalog item (any of the tenant's active items, in the block or not)?"""
    index = as_index(names)
    return any(w in index.by_word for w in words_of(sentence))


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


def _values(
    texts: Iterable[str], small_ok: bool = False, strict: bool = False, names: Iterable[str] = ()
) -> set[Decimal]:
    """The money figures in the customer's messages, read as anchored (a catalog item is in play);
    `strict` keeps figures beside units and identifiers too. Years and numbers in item names are
    never figures."""
    names = list(names)
    return {
        a.value
        for t in texts
        for a in customer_amounts(t, small_ok=small_ok, strict=strict, names=names)
    }


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
    offer_watch: bool = False,
) -> GuardVerdict:
    customer_texts = cap(customer_texts)
    items = grounding.items
    injected = {i.seq for i in items}
    all_prices = {Decimal(i.price) for i in items if i.price is not None}
    small_ok = any(p < 1000 for p in all_prices)
    catalog_names = grounding.name_index()  # built once per request, shared with offer detection
    by_seq = {i.seq: i for i in items}
    in_block = {i.name.lower() for i in items}
    outside_names = NameIndex(n for n in catalog_names.names if n.lower() not in in_block)
    sold_words = _sold_words(catalog_names)
    customer_money = _values(
        customer_texts, small_ok, strict=True, names=catalog_names
    )  # echo and laundering checks
    kb_money = _kb_values(kb_text, catalog_names, small_ok)
    kb_discounts = _kb_discount_sentences(kb_text)
    tiers = {Decimal(t) for t in tier_amounts}
    currency = (grounding.currency or "NGN").upper()
    matched_seqs = set(grounding.matched_seqs)
    matched = {Decimal(i.price) for i in items if i.seq in matched_seqs and i.price}
    ceiling = max(matched or all_prices, default=None)

    figure_cache: dict[str, set[Decimal]] = {}

    def figures_of(text: str) -> set[Decimal]:
        """The money figures of one customer message, computed once per call."""
        if text not in figure_cache:
            figure_cache[text] = _values([text], small_ok, names=catalog_names)
        return figure_cache[text]

    def _bargaining(texts) -> bool:
        figures = {v for t in texts for v in figures_of(t)}  # identifiers, deposits, units: no
        return (
            any(has_bargain_wording(t) for t in texts)
            or (ceiling is not None and any(v < ceiling for v in figures))
            # nothing priced in the block to compare with: any figure from a shop with items counts
            or (ceiling is None and bool(catalog_names) and bool(figures))
        )

    # an offer on THIS turn (the customer's latest words or the model's own signal) makes any plain
    # yes/okay an agreement; one from a turn or two ago (or the countdown) only does so in a
    # sentence about an item, a price or an offer
    short_reply = (
        bool(customer_texts) and len(customer_texts[-1].split()) <= 4
    )  # "ok", "yes please"
    offer_now = (
        offer_in_play
        or _bargaining(customer_texts[-1:])
        or (short_reply and _bargaining(customer_texts[-2:-1]))
    )
    offer_later = offer_watch or _bargaining(customer_texts[-3:-1])
    offer = offer_now or offer_later
    # accept / agree / deal count as agreement once the customer has bargained or named a figure
    # in the last three messages, whatever the figure is relative to the price
    agree_armed = offer or any(
        has_bargain_wording(t) or bool(figures_of(t)) for t in customer_texts[-3:]
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
    # tokens are judged on the reply as written: a fullwidth or zero-width disguise folds into a
    # valid-looking token in `text`, which code would then fail to resolve
    disguised = tokens.token_seqs(reply) != tokens.token_seqs(text)
    if grounding.strict:
        if tokens.token_seqs(text) or tokens.has_stray_markup(reply):
            violations.append(Violation(reply, "strict"))
    elif disguised or tokens.has_stray_markup(reply):
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
        # a bare name word ("2016" in "Honda Accord 2016") is not a mention of the item
        anchored = bool(seqs or names_catalog or _names_strongly(plain, items))
        masked = mask_non_money(plain, catalog_names)
        amounts = extract_amounts(masked, small_ok=small_ok, anchored=anchored)
        named_seqs = {i.seq for i in named}
        if not grounding.strict:
            bound = dict(bound_items(sentence, items, catalog_names))
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
                    if carried != {seq}:  # "It costs ..." after another item, or after two
                        violations.append(Violation(sentence, "token_mismatch"))
                elif pool is None and matched_seqs != {seq}:
                    # nothing in the sentence says whose price this is: only the one item the
                    # customer's question matched can be meant
                    violations.append(Violation(sentence, "token_mismatch"))
            if _says_sold_out_item_is_available(sentence, plain, items):
                violations.append(Violation(sentence, "sold_out_claim"))
            for seq in seqs:
                row = by_seq.get(seq)
                if (
                    row is not None
                    and row.availability == OfferingAvailability.OUT_OF_STOCK
                    and _unnegated(_AVAILABLE, sentence)
                ):
                    violations.append(Violation(sentence, "sold_out_claim"))
            if seqs and named_seqs:
                last = bound.get(seqs[-1])
                carried = last if last else _top_mentions(sentence, items)
            elif named_seqs:
                carried = _top_mentions(sentence, items)
            if (
                seqs
                and _bundle_hit(_without_names(plain, catalog_names), len(set(seqs)), sold_words)
                and not _PER.search(plain)
            ):
                violations.append(Violation(sentence, "bundle_price"))
            if (_unnegated(_FREE_PHRASE, _without_per_pair(sentence, plain, seqs))) or (
                (anchored or seqs or named)
                and (
                    _free_same_hit(sentence, plain, items, catalog_names)
                    or _free_beside_item(sentence, catalog_names, items)
                )
            ):
                violations.append(Violation(sentence, "freebie_wording"))
            if _SAME_ALONE.match(sentence):
                violations.append(Violation(sentence, "same_price_claim"))
            if (seqs or named) and _SAME.search(plain):
                violations.append(Violation(sentence, "same_price_claim"))
            if len(set(seqs)) == 1 and len(named_seqs) > 1 and re.search(r"\bboth\b", plain, re.I):
                violations.append(Violation(sentence, "same_price_claim"))
            folded = fold(sentence)
            if (
                lexicon.commits(folded)
                and any(not _negated(folded, m) for m in lexicon.lowering_matches(folded))
                and not _kb_states(kb_discounts, sentence)
            ):
                violations.append(Violation(sentence, "discount_promise"))
            if any(not _negated(folded, m) for m in lexicon.agreement_always_matches(folded)) or (
                agree_armed
                and any(not _negated(folded, m) for m in lexicon.agreement_matches(folded))
            ):
                violations.append(Violation(sentence, "agreement_after_offer"))
            # every catalog reply is judged: "I can give you a discount on it" names no item
            if _unnegated(_DISCOUNT_WORDS, sentence) and not _kb_states(kb_discounts, sentence):
                violations.append(Violation(sentence, "discount_wording"))
            # during the countdown a plain yes/okay beside nothing but a valid token of the item
            # ("Okay, the Black sneaker is [[price:#1]]. Shall I prepare your quote?") is fine
            only_a_token = (
                (not offer_now)
                and bool(seqs)
                and not amounts
                and not _names_catalog_strongly(plain, outside_names)
                and _top_mentions(sentence, items) <= set(seqs)
                and not _PRICE_TALK.search(tokens.TOKEN.sub(" ", sentence))
            )
            talks_price = offer_now or (
                not only_a_token
                and bool(
                    seqs
                    or amounts
                    or names_catalog
                    or _names_strongly(plain, items)
                    or _PRICE_TALK.search(sentence)
                )
            )
            if offer and (
                _unnegated(_AGREE_STRONG, sentence)
                or (
                    talks_price
                    and (_unnegated(_AGREE_WEAK, sentence) or _unnegated(_CUES, sentence))
                )
            ):
                violations.append(Violation(sentence, "agreement_after_offer"))
        for a in amounts:
            if a.kind == "percent":
                if (
                    not grounding.strict
                    and a.value not in percents
                    and (
                        _FIN_PERCENT.search(tokens.TOKEN.sub(" ", sentence))
                        or (anchored and not _SPEC_PERCENT.search(sentence))
                    )
                ):
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
