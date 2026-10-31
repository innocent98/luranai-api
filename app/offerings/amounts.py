"""Money amounts in free text: Nigerian forms, number words, percentages (used by the price guard).

Extraction is deliberately generous: a missed amount is a wrong price that reaches a customer, a
false amount costs one regeneration. `normalise` folds width and zero-width tricks, `mask_non_money`
hides things that look like amounts but are not (item names such as "Toyota Camry 2015" or
"N95 Mask", phone numbers, dates, reference codes), and `extract_amounts` does the rest.
"""

import re
from functools import lru_cache
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from app.offerings.names import NameIndex, as_index, lower_same_len

_NUM = r"(?:\d{1,3}(?:,\d{3})+|\d{1,3}(?:[   ]\d{3})+|\d+)(?:\.\d+)?"
_AMOUNT = re.compile(
    rf"""
    (?P<pre>(?:(?<![A-Za-z0-9])(?:₦|NGN|USD|GBP|EUR|[Nn](?=\s?\d))|[$£€])[\s-]?)?
    (?P<num>{_NUM})
    (?P<tail>
        \s?%
      | \s?per\s?cent\b | \s?percent\b
      | \s?(?:thousand|thou|million|billion|mil|bn|grand)\b
      | \s?[kK](?![A-Za-z0-9²]) | [mMbB](?![A-Za-z0-9²])
    )?
    (?P<cur>\s?(?:naira|ngn|kobo|dollars?|usd|pounds?|gbp|euros?|eur)\b)?
    """,
    re.VERBOSE | re.IGNORECASE,
)
_SPEC_AFTER = {
    "uhd", "hd", "ultra", "resolution", "video", "videos", "display", "screen", "tv", "monitor",
    "hdr", "oled", "qled", "fps", "camera", "panel", "projector",
}  # fmt: skip
_SPEC_BEFORE = re.compile(
    r"\b(?:supports?|supporting|plays?|records?|resolution|ultra|uhd)\s+$", re.IGNORECASE
)
_MULT = {
    "k": 10**3,
    "thousand": 10**3,
    "thou": 10**3,
    "grand": 10**3,
    "m": 10**6,
    "mil": 10**6,
    "million": 10**6,
    "b": 10**9,
    "bn": 10**9,
    "billion": 10**9,
}
_PRE_CCY = {
    "#": "NGN",
    "₦": "NGN",
    "n": "NGN",
    "ngn": "NGN",
    "$": "USD",
    "usd": "USD",
    "£": "GBP",
    "gbp": "GBP",
    "€": "EUR",
    "eur": "EUR",
}
_CUR_CCY = {
    "naira": "NGN",
    "ngn": "NGN",
    "kobo": "NGN",
    "dollar": "USD",
    "dollars": "USD",
    "usd": "USD",
    "pound": "GBP",
    "pounds": "GBP",
    "gbp": "GBP",
    "euro": "EUR",
    "euros": "EUR",
    "eur": "EUR",
}
_UNIT_AFTER_M = {
    "wide",
    "long",
    "by",
    "high",
    "tall",
    "deep",
    "away",
    "x",
    "sq",
    "square",
    "metre",
    "metres",
    "meter",
    "meters",
    "radius",
    "apart",
    "from",
    "plot",
}
_UNIT_AFTER = {
    "pair",
    "pairs",
    "piece",
    "pieces",
    "pcs",
    "item",
    "items",
    "unit",
    "units",
    "day",
    "days",
    "hour",
    "hours",
    "hr",
    "hrs",
    "week",
    "weeks",
    "month",
    "months",
    "year",
    "years",
    "size",
    "sizes",
    "colour",
    "colours",
    "color",
    "colors",
    "inch",
    "inches",
    "cm",
    "mm",
    "kg",
    "g",
    "photo",
    "photos",
    "people",
    "person",
    "persons",
    "slot",
    "slots",
    "left",
    "available",
    "times",
    "minute",
    "minutes",
    "min",
    "mins",
    "sec",
    "secs",
    "am",
    "pm",
    "of",
    "x",
    "guest",
    "guests",
    "pax",
    "portion",
    "portions",
    "serving",
    "servings",
    "plate",
    "plates",
    "slice",
    "slices",
    "km",
    "room",
    "rooms",
    "seat",
    "seats",
    "customer",
    "customers",
    "order",
    "orders",
    "kid",
    "kids",
    "child",
    "children",
    "adult",
    "adults",
    "staff",
    "members",
    "workers",
    "litre",
    "litres",
    "liter",
    "liters",
    "gb",
    "mb",
    "bag",
    "bags",
    "box",
    "boxes",
    "pack",
    "packs",
    "carton",
    "cartons",
    "dozen",
    "metres",
    "meters",
    "bedroom",
    "bedrooms",
    "floor",
    "floors",
    "years-old",
}
_PRICE_CUE = re.compile(
    r"(?:cost|costs|costing|price|prices|priced|pay|paying|total|fee|fees|charge|charged|worth"
    r"|do|give|offer|sell|(?:go|goes|sell|sells|sold)\s+for)\s+"
    r"(?:(?:is|are|of|was|at|just|only|about|around|approximately|roughly|to|you|it|them|that|the)\s+){0,3}$",
    re.IGNORECASE,
)
_KEYED = re.compile(r"[:=@\-–—]\s*$")  # "Red Bag: 25000", "Red Bag - 25,000"
_WEAK_CUE = re.compile(r"\b(?:is|are|was|at|for|from|only|about|around|just)\s+$", re.IGNORECASE)
_MONEY_FOLLOW = {"only", "naira", "ngn", "each", "per", "kobo"}
_YEAR_CUE = re.compile(
    r"(?:\b(?:in|since|year|during|copyright|registered|made|built|build|model|edition|version"
    r"|released|vintage|manufactured|a|an|jan|feb|mar|apr|may|"
    r"jun|jul|aug|sep|sept|oct|nov|dec|january|february|march|april|june|july|august|september|"
    r"october|november|december)|©)\s*$",
    re.IGNORECASE,
)

_ONES = {
    w: i
    for i, w in enumerate(
        "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
        "sixteen seventeen eighteen nineteen".split()
    )
}
_TENS = {
    w: 10 * (i + 2)
    for i, w in enumerate("twenty thirty forty fifty sixty seventy eighty ninety".split())
}
_BIG = {"thousand": 10**3, "million": 10**6, "billion": 10**9}
_WORD = re.compile(r"[A-Za-z]+")
_NUMWORDS = set(_ONES) | set(_TENS) | set(_BIG) | {"hundred"}


@dataclass(frozen=True)
class Amount:
    value: Decimal
    kind: str  # "money" | "percent"
    currency: str | None
    start: int
    end: int
    mult: int = 1  # magnitude of a digit amount with a word tail ("12 thousand")


_YEAR_AFTER = {"model", "models", "year", "edition", "version", "build", "series", "release", "one"}


def _is_year_like(digits: str, before: str, after: str = "") -> bool:
    """A four-digit number that reads as a year: a year cue before it, or "2016 model" after it."""
    return (
        len(digits) == 4
        and 1900 <= int(digits) <= 2100
        and (bool(_YEAR_CUE.search(before)) or after in _YEAR_AFTER)
    )


def _next_word(text: str, pos: int) -> str:
    m = re.match(r"\s*([A-Za-z²]+)", text[pos:])
    return m.group(1).lower() if m else ""


def _digit_amounts(text: str, small_ok: bool, ref_seqs: frozenset, anchored: bool) -> list[Amount]:
    found: list[tuple[Amount, bool]] = []  # (amount, had_magnitude)
    for m in _AMOUNT.finditer(text):
        raw_num = m.group("num")
        digits = re.sub(r"[ ,  ]", "", raw_num)
        pre = (m.group("pre") or "").strip().lower()
        tail = (m.group("tail") or "").strip().lower().replace(" ", "")
        cur = (m.group("cur") or "").strip().lower()
        before = text[max(0, m.start() - 80) : m.start()]  # a window: never rescan the prefix
        grouped = bool(re.search(r"[,   ]", raw_num))
        if (
            not pre
            and not tail
            and not cur
            and not grouped
            and m.start() > 0
            and text[m.start() - 1].isalpha()
        ):
            found.append((Amount(Decimal(0), "skip", None, m.start(), m.end()), False))
            continue  # "A2894", "S23": a model number or code, not an amount
        if len(digits) > 15:  # not an amount of money, and too long to be worth parsing
            found.append((Amount(Decimal(0), "skip", None, m.start(), m.end()), False))
            continue
        value = Decimal(digits)
        grouped = bool(re.search(r"[,   ]", raw_num))
        end = m.end()

        if tail == "k" and re.fullmatch(r"[2-8]", digits) and not pre and not cur:
            nxt = _next_word(text, m.end())
            if nxt in _SPEC_AFTER or _SPEC_BEFORE.search(before):
                found.append((Amount(Decimal(0), "skip", None, m.start(), m.end()), False))
                continue  # "4K UHD", "supports 4K": a resolution, not four thousand naira

        if tail == "%" or re.fullmatch(r"per\s?cent|percent", tail):
            found.append((Amount(value, "percent", None, m.start(), end), False))
            continue

        mult = _MULT.get(tail, 1) if tail else 1
        if tail in ("m", "b", "bn") and not pre and not cur:
            # "50m" is metres, "5b" a size: a million or billion needs a currency mark or the word
            mult, tail, end = 1, "", m.start("tail")
        has_marker = bool(pre or cur or tail)
        glued = re.match(r"[A-Za-z]", text[m.end() : m.end() + 1] or "")
        if glued and not (tail or cur or pre or grouped):
            continue  # "5pm", "42cm": a bare number glued to a word
        if before.endswith("#") and not pre:
            # "#12" is an item ref; "#9,000", "#9k", "#9000" is how many Nigerians write naira
            seq_like = not (tail or grouped) and digits.isdigit() and int(digits) in ref_seqs
            as_money = not seq_like and (
                grouped
                or bool(tail)
                or len(digits) >= 4
                or (small_ok and 100 <= value <= 999 and "." not in raw_num)
            )
            if not as_money:
                continue
            pre = "#"
            has_marker = True

        if has_marker:
            money = True
        else:
            money = False
            has_decimal = "." in raw_num
            big = grouped or (
                not has_decimal and not digits.startswith("0") and 4 <= len(digits) < 10
            )
            follow = _next_word(text, m.end())
            near = (
                bool(_PRICE_CUE.search(before) or _KEYED.search(before) or _WEAK_CUE.search(before))
                or follow in _MONEY_FOLLOW
                or anchored
            )
            if (
                big
                and near
                and len(digits) < 12
                and follow not in _UNIT_AFTER
                and not _is_year_like(digits, before, follow)
                and not before.rstrip().endswith("+")
            ):
                money = True  # "Plot 1234" and "2000 customers" have no price context: not money
            if (
                not money
                and not has_decimal
                and not digits.startswith("0")
                and not before.rstrip().endswith("+")
            ):
                tail_word = _next_word(text, m.end())
                if (
                    _PRICE_CUE.search(before)
                    and tail_word not in _UNIT_AFTER
                    and not text[m.end() : m.end() + 1] == ":"
                ):
                    money = True
            if (
                not money
                and small_ok
                and not has_decimal
                and not digits.startswith("0")
                and 100 <= value <= 999
                and _next_word(text, m.end()) not in _UNIT_AFTER
                and text[m.end() : m.end() + 1] != ":"
                and not before.rstrip().endswith("+")
            ):
                money = True  # a catalog with prices under 1,000: "800 works" is an amount
            if not money:
                found.append((Amount(value, "skip", None, m.start(), end), False))
                continue
        ccy = _PRE_CCY.get(pre) or _CUR_CCY.get(cur)
        value = value * mult
        if cur == "kobo":
            value = value / 100
        found.append(
            (Amount(value, "money", ccy, m.start(), end, mult), bool(tail) and tail != "%")
        )

    # "10-12k": the bare first figure shares the second's magnitude
    for i, (a, _mag) in enumerate(found[:-1]):
        nxt, nxt_mag = found[i + 1]
        if (
            a.kind == "skip"
            and nxt_mag
            and nxt.kind == "money"
            and re.fullmatch(r"\s*(?:-|–|—|to|and|or)\s*", text[a.end : nxt.start] or "")
        ):
            tail_m = _AMOUNT.match(text, nxt.start)
            tail = (tail_m.group("tail") or "").strip().lower() if tail_m else ""
            found[i] = (
                Amount(
                    a.value * _MULT.get(tail.replace(" ", ""), 1),
                    "money",
                    nxt.currency,
                    a.start,
                    a.end,
                ),
                True,
            )
    return [a for a, _ in found if a.kind != "skip"]


def _word_amounts(text: str, taken: list[Amount]) -> list[Amount]:
    covered = bytearray(len(text) + 1)  # one pass: which characters a digit amount already owns
    for t in taken:
        covered[max(t.start, 0) : t.end] = b"\x01" * max(t.end - max(t.start, 0), 0)
    words = [
        (m.group().lower(), m.start(), m.end())
        for m in _WORD.finditer(text)
        if not all(covered[m.start() : m.end()])
    ]
    out: list[Amount] = []
    i = 0
    while i < len(words):
        w, s, _e = words[i]
        if w not in _NUMWORDS:
            i += 1
            continue
        j = i
        while j + 1 < len(words):
            nw, ns, _ne = words[j + 1]
            gap = text[words[j][2] : ns]
            if not re.fullmatch(r"[\s-]+", gap):
                break
            if nw in _NUMWORDS:
                j += 1
            elif (
                nw == "and"
                and j + 2 < len(words)
                and words[j + 2][0] in _NUMWORDS
                and re.fullmatch(r"\s+", text[ns + 3 : words[j + 2][1]])
            ):
                j += 2
            else:
                break
        run = [words[k][0] for k in range(i, j + 1) if words[k][0] != "and"]
        start, end = s, words[j][2]
        i = j + 1
        total = current = 0
        for token in run:
            if token in _ONES:
                current += _ONES[token]
            elif token in _TENS:
                current += _TENS[token]
            elif token == "hundred":
                current = (current or 1) * 100
            else:
                total += (current or 1) * _BIG[token]
                current = 0
        value = total + current
        nxt = re.match(
            r"\s*(naira|ngn|kobo|dollars?|usd|pounds?|euros?)\b", text[end:], re.IGNORECASE
        )
        tail_k = re.match(r"[\s-]?(?:k|grand|thou)\b", text[end:], re.IGNORECASE)
        if tail_k:  # "fifteen k", "five grand"
            value *= 1000
            end += tail_k.end()
        has_big = any(t in _BIG for t in run)
        money = bool(has_big or nxt or tail_k or value >= 1000)
        ccy = _CUR_CCY.get(nxt.group(1).lower()) if nxt else None
        dec = Decimal(value)
        if nxt and nxt.group(1).lower() == "kobo":
            dec = dec / 100
        out.append(
            Amount(dec, "money" if money else "skip", ccy, start, end + (nxt.end() if nxt else 0))
        )
    return out


_INVISIBLE = re.compile("[\u00ad\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff]")


_COMMA_DECIMAL = re.compile(r"(?<![\d,])(\d{1,3}),(\d{1,2})(?=\s?[kKmM](?![A-Za-z]))")


def normalise(text: str | None) -> str:
    """Fold width/compat forms (fullwidth digits), drop zero-width characters, read `2,5k` as 2.5k."""
    folded = _INVISIBLE.sub("", unicodedata.normalize("NFKC", text or ""))
    return _COMMA_DECIMAL.sub(r"\1.\2", folded)


_PHONE = re.compile(
    r"(?<!\d)(?:\+?234|0)\d{2,3}[\s-]?\d{3}[\s-]?\d{4}(?!\d)|(?<!\d)\d{3,4}[\s-]\d{3}[\s-]\d{4}(?!\d)"
)
_DATE = re.compile(r"(?<!\d)\d{1,2}[/-]\d{1,2}[/-]\d{2,4}(?!\d)")
_REFCODE = re.compile(r"\b(?!(?:ngn|usd|gbp|eur)-)[A-Za-z]{2,5}-\d{2,6}\b", re.IGNORECASE)


def find_all(haystack: str, needle: str):
    """Every start of `needle` in `haystack` (case-insensitive) with `str.find`: no regex is built
    per name, so a catalog of thousands of items cannot flood Python's regex cache."""
    if not needle:
        return
    low, key = lower_same_len(haystack), lower_same_len(needle)
    i = low.find(key)
    while i != -1:
        yield i
        i = low.find(key, i + len(key))


def blank_names(text: str, names: Iterable[str]) -> str:
    """`text` with every occurrence of any name replaced by spaces (same length), in one pass.
    With a `NameIndex` only the names that share a word with the text are looked for."""
    if isinstance(names, NameIndex):
        return names.blank(text)
    spans = [(i, i + len(n)) for n in {n for n in names if n} for i in find_all(text, n)]
    if not spans:
        return text
    chars = list(text)
    for a, b in spans:
        chars[a:b] = " " * (b - a)
    return "".join(chars)


def mask_non_money(text: str, names: Iterable[str] = ()) -> str:
    """Blank out spans that carry digits but are not amounts, keeping every offset stable."""
    text = normalise(text)

    def blank(m: re.Match) -> str:
        return " " * (m.end() - m.start())

    text = blank_names(text, names)
    for pattern in (_PHONE, _DATE, _REFCODE):
        text = pattern.sub(blank, text)
    return text


def _merge_word_tails(digit: list[Amount], words: list[Amount], text: str) -> list[Amount]:
    """ "12 thousand five hundred": the words after a magnitude add to the digit amount."""
    merged = list(digit)
    for w in words:
        for i, d in enumerate(merged):
            if (
                d.mult >= 1000
                and d.kind == "money"
                and w.value < d.mult
                and re.fullmatch(r"\s+(?:and\s+)?", text[d.end : w.start])
            ):
                merged[i] = Amount(d.value + w.value, "money", d.currency, d.start, w.end, d.mult)
                w = None
                break
        if w is not None:
            merged.append(w)
    return merged


def extract_amounts(
    text: str,
    *,
    small_ok: bool = False,
    ref_seqs: frozenset = frozenset(),
    anchored: bool = False,
) -> list[Amount]:
    """Every amount in `text`. `small_ok`: count bare 3-digit numbers (a catalog under 1,000);
    `ref_seqs`: item numbers, so that `#12` stays a reference; `anchored`: the sentence names a
    catalog item or carries a price token, so a bare grouped or 4+ digit number is a price."""
    text = normalise(text)
    digit = _digit_amounts(text, small_ok, ref_seqs, anchored)
    taken = digit
    words = _word_amounts(text, taken)
    combined = _merge_word_tails(digit, words, text)
    return sorted((a for a in combined if a.kind != "skip"), key=lambda a: a.start)


# -- what a customer wrote: figures, read as anchored once an item is in play ---------------------

_UNITS = (
    r"pcs?|pieces?|pairs?|units?|items?|orders?|guests?|people|persons?|pax|portions?|servings?"
    r"|plates?|rooms?|seats?|customers?|kids?|children|adults?|sizes?|km|kg|cm|mm|mins?|minutes?"
    r"|hrs?|hours?|days?|weeks?|months?|years?|yrs?|gb|mb|litres?|liters?|times|bags?|boxes|packs?"
    r"|cartons?|dozens?|metres?|meters?|inches|inch|yards?|bedrooms?|floors?|staff|workers|members"
)
_NOT_AN_OFFER = re.compile(
    r"\b(?:size|sizes|no\.|number|order(?:\s{1,3}(?:number|no\.?|id))?|ref|invoice|ticket|account"
    r"|acct|room|flat|house|apt|plot|block|code|pin|otp)\b\s{0,3}(?:is\s{0,3})?[:#]?\s{0,3}\d{1,15}(?:-\d{1,15})?"
    rf"|\b\d[\d,.]{{0,12}}\s{{0,3}}(?:{_UNITS})\b(?:\s{{0,3}}old)?"
    r"|\b(?:x|qty|quantity)\s{0,3}\d{1,15}|\b\d{1,15}\s{0,3}x\b"
    r"|\b\d{1,15}\s{1,3}(?:in\s{1,3}stock|available|left|remaining)\b"
    r"|\b(?!(?:n|ngn|rs|usd|gbp|eur)-?\d)[A-Za-z]{1,3}-?\d{1,5}\b"
    r"|\b(?:paid|deposited|transferred|sent|payment\s{1,3}of)\b\s{0,3}(?:of\s{1,3})?[\w₦#]{0,10}\s{0,3}\d[\d,.]{0,12}k?"
    r"|\bthe\s{1,3}[₦#N]?\s{0,2}\d[\d,.]{0,12}k?\s{0,2}(?:one|ones|model|version|pair|option|size)\b"
    r"|\b(?:at|by|around|before|after)\s{1,3}\d{1,2}(?:(?::\d\d)\s{0,3}(?:am|pm)?|\s{0,3}(?:am|pm|o'clock|hours))\b"
    r"|\b\d{1,15}\s{1,3}(?:[a-z]{1,20}\s{1,3}){1,3}(?:avenue|ave|street|st|road|rd|close|crescent|way|drive|lane|estate|layout)\b",
    re.IGNORECASE,
)


_BARE_YEAR = re.compile(
    r"(?<![\d,.₦#$£€])\b(19[5-9]\d|20[0-3]\d)\b(?![\d,.]|\s{0,2}k\b)", re.IGNORECASE
)
_YEAR_BEFORE = re.compile(
    r"\b(?:the|a|an|or|any|which|that|this|model|year|built|build|registered|made|edition|version"
    r"|released|vintage)\s{1,3}$",
    re.IGNORECASE,
)
_YEAR_AFTER_RE = re.compile(
    r"^\s{0,3}(?:model|models|year|edition|version|build|series|release|one|or\b|\?)", re.IGNORECASE
)


_PRICE_BEFORE = re.compile(
    r"\b(?:pay|do|take|final|sell|give|offer|collect|abeg|make|naira|accept)\s{0,2}[₦#N]?\s{0,2}$"
    r"|[₦#]\s{0,2}$",
    re.IGNORECASE,
)


def _mask_years(text: str) -> str:
    """ "a 2016 Camry", "the 2019 one", "2018 or 2017?": bare years are not offers."""

    def blank(m: re.Match) -> str:
        before, after = text[max(0, m.start() - 12) : m.start()], text[m.end() : m.end() + 12]
        if _PRICE_BEFORE.search(before):
            return m.group(0)  # "I can pay 2018 for it": offer wording right before: keep it
        if _YEAR_BEFORE.search(before) or _YEAR_AFTER_RE.match(after):
            return " " * len(m.group(0))
        return m.group(0)

    return _BARE_YEAR.sub(blank, text)


def _mask_quantities(text: str, names: Iterable[str]) -> str:
    """ "50 sausage rolls", "2 chocolate cakes": a bare count written before an item's name is a
    quantity. A figure written after the name ("black sneaker 9000") stays a price."""
    index = as_index(names)
    words = index.quantity_words
    if not words:
        return text
    stems = {x.rstrip("s") for x in words}

    def blank(m: re.Match) -> str:
        nxt = re.findall(r"[a-z]+", m.group("rest").lower())[:2]
        return " " * len(m.group(0)) if any(w.rstrip("s") in stems for w in nxt) else m.group(0)

    return re.sub(
        r"(?<![\d,.₦#$£€])\b\d{1,3}(?P<rest>\s{1,3}[A-Za-z]+(?:\s{1,3}[A-Za-z]+)?)", blank, text
    )


_NAME_NUMBER = re.compile(r"(?<![\d,.₦#$£€])\b\d+\b(?![\d,]|\s{0,2}k\b)")


@lru_cache(maxsize=8192)
def _numbers_of(name: str) -> frozenset[str]:
    return frozenset(re.findall(r"\d+", name))


def mask_for_offers(text: str | None, names: Iterable[str] = ()) -> str:
    """The customer's text with phones, dates, item names, years, units and identifiers blanked."""
    names = as_index(names)
    base = _mask_years(mask_non_money(text, names))
    return _NOT_AN_OFFER.sub(lambda m: " " * len(m.group(0)), _mask_quantities(base, names))


def customer_amounts(
    text: str | None,
    *,
    small_ok: bool = True,
    strict: bool = False,
    names: Iterable[str] = (),
) -> list[Amount]:
    """The money figures in something a customer wrote, once a catalog item is in play: a bare
    "abeg 9000" is an amount; "size 44", "order 10021", "150 guests", phone numbers, a year
    ("a 2016 Camry"), a number inside an item's name and a count before an item noun ("50
    sausage rolls") are not.

    `strict` keeps the figures next to units and identifiers too (only phones, dates, item names
    and years are dropped): the guard reads the customer that way, so a masked figure can never
    hide an offer from the agreement rules; only the code-only reply decision uses the full mask."""
    names = as_index(names)
    base = _mask_years(mask_non_money(text, names))
    numbers = names.numbers
    if numbers:
        # "camry 2018", "a1500", "840 g5": the number is part of an item's name, not an offer
        # (unless offer wording sits right before it); one pass, no regex built per number
        def blank_number(m: re.Match, b: str = base) -> str:
            if m.group(0) in numbers and not _PRICE_BEFORE.search(
                b[max(0, m.start() - 12) : m.start()]
            ):
                return " " * len(m.group(0))
            return m.group(0)

        base = _NAME_NUMBER.sub(blank_number, base)
    if not strict:
        base = _NOT_AN_OFFER.sub(lambda m: " " * len(m.group(0)), _mask_quantities(base, names))
    return [a for a in extract_amounts(base, small_ok=small_ok, anchored=True) if a.kind == "money"]
