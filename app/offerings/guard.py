"""The price guard (spec 7.5): code, not prompt, decides whether a reply may be sent.

It extracts every money amount from the reply (Nigerian forms: `₦25,000`, `N25000`, `25k`, `2.5m`,
`25,000 naira`, "twelve thousand naira") and checks each sentence against what the model was
actually shown this turn. A listed price only counts for the item it belongs to, a customer's own
figure may be repeated back but never agreed to (decision D1), and a knowledge-base figure never
sits beside an item name (a stale KB price must not beat the catalog). This module only judges; the
regenerate-then-fall-back flow is `guard_flow.py`.
"""

import re
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal
from collections.abc import Iterable

from app.offerings.facts import OfferingFacts
from app.offerings.grounding import GroundingResult, singular

# -- extraction ----------------------------------------------------------------------------

_NUM = r"(?:\d{1,3}(?:,\d{3})+|\d{1,3}(?:[   ]\d{3})+|\d+)(?:\.\d+)?"
_AMOUNT = re.compile(
    rf"""
    (?P<pre>(?:(?<![A-Za-z0-9])(?:₦|NGN|USD|GBP|EUR|[Nn](?=\s?\d))|[$£€])\s?)?
    (?P<num>{_NUM})
    (?P<tail>
        \s?%
      | \s?per\s?cent\b | \s?percent\b
      | \s?(?:thousand|million|billion|mil|bn)\b
      | \s?[kK](?![A-Za-z0-9²]) | [mMbB](?![A-Za-z0-9²])
    )?
    (?P<cur>\s?(?:naira|ngn|kobo|dollars?|usd|pounds?|gbp|euros?|eur)\b)?
    """,
    re.VERBOSE | re.IGNORECASE,
)
_MULT = {
    "k": 10**3,
    "thousand": 10**3,
    "m": 10**6,
    "mil": 10**6,
    "million": 10**6,
    "b": 10**9,
    "bn": 10**9,
    "billion": 10**9,
}
_PRE_CCY = {
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


def _is_year_like(digits: str, before: str) -> bool:
    return len(digits) == 4 and 1900 <= int(digits) <= 2100 and bool(_YEAR_CUE.search(before))


def _next_word(text: str, pos: int) -> str:
    m = re.match(r"\s*([A-Za-z²]+)", text[pos:])
    return m.group(1).lower() if m else ""


def _digit_amounts(text: str) -> list[Amount]:
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
        if not tail and not cur and re.match(r"[A-Za-z]", text[m.end() : m.end() + 1] or ""):
            continue  # "5pm", "42cm": a number glued to a word
        if before.rstrip().endswith("#") and not pre:
            continue  # item ref

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
            if not money:
                found.append((Amount(value, "skip", None, m.start(), end), False))
                continue
        ccy = _PRE_CCY.get(pre) or _CUR_CCY.get(cur)
        value = value * mult
        if cur == "kobo":
            value = value / 100
        found.append((Amount(value, "money", ccy, m.start(), end), bool(tail) and tail != "%"))

    # "10-12k": the bare first figure shares the second's magnitude
    for i, (a, _mag) in enumerate(found[:-1]):
        nxt, nxt_mag = found[i + 1]
        if (
            a.kind == "skip"
            and nxt_mag
            and nxt.kind == "money"
            and re.fullmatch(r"\s*(?:-|–|—|to)\s*", text[a.end : nxt.start] or "")
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
        has_big = any(t in _BIG for t in run)
        if not (has_big or nxt):
            continue
        ccy = _CUR_CCY.get(nxt.group(1).lower()) if nxt else None
        dec = Decimal(value)
        if nxt and nxt.group(1).lower() == "kobo":
            dec = dec / 100
        out.append(Amount(dec, "money", ccy, start, end + (nxt.end() if nxt else 0)))
    return out


_INVISIBLE = re.compile("[\u00ad\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff]")


def normalise(text: str | None) -> str:
    """Fold width/compat forms (fullwidth digits) and drop zero-width characters."""
    return _INVISIBLE.sub("", unicodedata.normalize("NFKC", text or ""))


def extract_amounts(text: str) -> list[Amount]:
    text = normalise(text)
    digit = _digit_amounts(text)
    return sorted(digit + _word_amounts(text, digit), key=lambda a: a.start)


# -- judging ------------------------------------------------------------------------------

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


_ECHO = re.compile(
    r"\b(?:you(?:'ve|\s+have)?\s+(?:said|mentioned|offered|asked|told|wrote|quoted|suggested|"
    r"proposed|stated|shared)|you(?:'re|\s+are)\s+(?:offering|proposing|asking|suggesting)"
    r"|your\s+(?:offer|offered|figure|price|proposal|bid|quote|suggestion)|noted\s+(?:your|the)"
    r"|as\s+you\s+(?:said|mentioned)|you(?:'d|\s+would)\s+(?:like|want)\s+to\s+pay|budget)\b",
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


def _echoed(sentence: str, amount: Amount) -> bool:
    """The sentence attributes the figure to the customer, right beside it."""
    return bool(_ECHO.search(sentence[max(0, amount.start - 60) : amount.end + 20]))


def _name_tokens(item: OfferingFacts) -> set[str]:
    return {
        singular(t)
        for t in re.findall(r"[a-z0-9]+", item.name.lower())
        if len(t) >= 3 and t not in _NAME_STOP
    }


def _named_items(
    sentence: str, items: list[OfferingFacts]
) -> tuple[list[OfferingFacts], list[OfferingFacts]]:
    """(loose, strict): every item the sentence touches, and the best-matching subset."""
    lowered = sentence.lower()
    words = {singular(w) for w in re.findall(r"[a-z0-9]+", lowered)}
    refs = {int(n) for n in _REF.findall(sentence)}
    scored: list[tuple[float, OfferingFacts, bool]] = []
    for item in items:
        by_ref = item.seq in refs
        full = bool(item.name) and item.name.lower() in lowered
        tokens = _name_tokens(item)
        hit = len(tokens & words)
        frac = hit / len(tokens) if tokens else 0.0
        if by_ref or full or hit:
            scored.append((1.0 if (by_ref or full) else frac, item, by_ref or full))
    if not scored:
        return [], []
    loose = [i for _, i, _ in scored]
    explicit = [i for _, i, exact in scored if exact]
    if explicit:
        return loose, explicit
    best = max(f for f, _, _ in scored)
    return loose, [i for f, i, _ in scored if f == best]


def _values(texts: Iterable[str], kind: str) -> set[Decimal]:
    return {a.value for t in texts for a in extract_amounts(t) if a.kind == kind}


def _has(values: Iterable, v: Decimal) -> bool:
    return any(v == x for x in values)


def check_reply(
    reply: str,
    grounding: GroundingResult,
    *,
    customer_texts: Iterable[str] = (),
    kb_text: str = "",
    tier_amounts: Iterable[int] = (),
) -> GuardVerdict:
    customer_texts = list(customer_texts)
    customer_money = _values(customer_texts, "money")
    customer_pct = _values(customer_texts, "percent")
    kb_money = _values([kb_text], "money")
    kb_pct = _values([kb_text], "percent")
    tiers = {Decimal(t) for t in tier_amounts}
    items = grounding.items
    all_prices = {Decimal(i.price) for i in items if i.price is not None}
    currency = (grounding.currency or "NGN").upper()

    violations: list[Violation] = []
    for sentence in (s.strip() for s in _SENTENCE.split(normalise(reply))):
        amounts = extract_amounts(sentence)
        if not amounts:
            continue
        loose, strict = _named_items(sentence, items)
        listed = {Decimal(i.price) for i in strict if i.price is not None} if strict else all_prices
        cue = bool(_CUES.search(sentence))
        for a in amounts:
            reason = None
            if grounding.strict:
                reason = "strict"
            elif a.kind == "percent":
                ok = _has(customer_pct, a.value) or (not loose and _has(kb_pct, a.value))
                reason = None if ok else "percent"
            elif a.currency and a.currency != currency:
                reason = "currency"
            else:
                kb_ok = _has(kb_money, a.value)
                tier_ok = _has(tiers, a.value)
                kb_only = kb_ok and not loose
                echoed = _echoed(sentence, a)
                if (
                    _has(customer_money, a.value)
                    and not strict
                    and not echoed
                    and not (tier_ok or kb_only)
                ):
                    # with no item named, a listed price that happens to equal the customer's own
                    # figure must not read as agreeing to it
                    reason = "unattributed_customer_amount"
                elif strict and _has(listed, a.value):
                    reason = None  # the named item's own listed price, whatever the wording
                elif cue and not tier_ok and not kb_only:
                    # concession language beside any other figure, including another item's price
                    reason = "concession"
                elif not strict and _has(all_prices, a.value):
                    reason = None
                elif tier_ok or kb_only:
                    reason = None
                elif _has(customer_money, a.value):
                    # the customer's own figure may be quoted back, never silently adopted (D1)
                    reason = None if echoed else "unattributed_customer_amount"
                elif kb_ok:
                    reason = "kb_beside_item"
                else:
                    reason = "wrong_item_price" if loose else "not_in_block"
            if reason:
                violations.append(Violation(sentence, reason, [a.value]))
    return GuardVerdict(ok=not violations, violations=violations)
