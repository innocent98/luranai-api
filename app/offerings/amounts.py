"""Money amounts in free text: Nigerian forms, number words, percentages (used by the price guard).

Extraction is deliberately generous: a missed amount is a wrong price that reaches a customer, a
false amount costs one regeneration. `normalise` folds width and zero-width tricks, `mask_non_money`
hides things that look like amounts but are not (item names such as "Toyota Camry 2015" or
"N95 Mask", phone numbers, dates, reference codes), and `extract_amounts` does the rest.
"""

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

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
}
_PRICE_CUE = re.compile(
    r"(?:cost|costs|costing|price|prices|priced|pay|paying|total|fee|fees|charge|charged|worth"
    r"|do|give|offer|sell|(?:go|goes|sell|sells|sold)\s+for)\s+"
    r"(?:(?:is|are|of|was|at|just|only|about|around|approximately|roughly|to|you|it|them|that|the)\s+){0,3}$",
    re.IGNORECASE,
)
_YEAR_CUE = re.compile(
    r"(?:\b(?:in|since|year|during|copyright|jan|feb|mar|apr|may|"
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


def _is_year_like(digits: str, before: str) -> bool:
    return len(digits) == 4 and 1900 <= int(digits) <= 2100 and bool(_YEAR_CUE.search(before))


def _next_word(text: str, pos: int) -> str:
    m = re.match(r"\s*([A-Za-z²]+)", text[pos:])
    return m.group(1).lower() if m else ""


def _digit_amounts(text: str, small_ok: bool, ref_seqs: frozenset) -> list[Amount]:
    found: list[tuple[Amount, bool]] = []  # (amount, had_magnitude)
    for m in _AMOUNT.finditer(text):
        raw_num = m.group("num")
        digits = re.sub(r"[ ,  ]", "", raw_num)
        pre = (m.group("pre") or "").strip().lower()
        tail = (m.group("tail") or "").strip().lower().replace(" ", "")
        cur = (m.group("cur") or "").strip().lower()
        before = text[: m.start()]
        value = Decimal(digits)
        grouped = bool(re.search(r"[,   ]", raw_num))
        end = m.end()

        if tail == "%" or re.fullmatch(r"per\s?cent|percent", tail):
            found.append((Amount(value, "percent", None, m.start(), end), False))
            continue

        mult = _MULT.get(tail, 1) if tail else 1
        if (
            tail in ("m", "b", "bn")
            and not pre
            and not cur
            and (
                tail != "m"
                or _next_word(text, m.end()) in _UNIT_AFTER_M
                or text[m.end() : m.end() + 1] == "²"
            )
        ):
            mult = 1
            tail = ""
            end = m.start("tail")
        if tail == "b" and not pre:
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

        if has_marker or grouped:
            money = True
        else:
            money = False
            has_decimal = "." in raw_num
            if (
                not has_decimal
                and not digits.startswith("0")
                and len(digits) >= 4
                and len(digits) < 10
            ):
                if not _is_year_like(digits, before) and not before.rstrip().endswith("+"):
                    money = True
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
    words = [
        (m.group().lower(), m.start(), m.end())
        for m in _WORD.finditer(text)
        if not any(t.start <= m.start() and m.end() <= t.end for t in taken)
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


def mask_non_money(text: str, names: Iterable[str] = ()) -> str:
    """Blank out spans that carry digits but are not amounts, keeping every offset stable."""
    text = normalise(text)

    def blank(m: re.Match) -> str:
        return " " * (m.end() - m.start())

    for name in sorted({n for n in names if n}, key=len, reverse=True):
        text = re.sub(re.escape(name), blank, text, flags=re.IGNORECASE)
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
    text: str, *, small_ok: bool = False, ref_seqs: frozenset = frozenset()
) -> list[Amount]:
    """Every amount in `text`. `small_ok`: count bare 3-digit numbers (a catalog under 1,000);
    `ref_seqs`: item numbers, so that `#12` stays a reference."""
    text = normalise(text)
    digit = _digit_amounts(text, small_ok, ref_seqs)
    taken = digit
    words = _word_amounts(text, taken)
    combined = _merge_word_tails(digit, words, text)
    return sorted((a for a in combined if a.kind != "skip"), key=lambda a: a.start)
