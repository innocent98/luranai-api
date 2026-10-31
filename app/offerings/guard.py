"""The price guard (spec 7.5): code, not prompt, decides whether a reply may be sent.

Every money amount in the reply (see `amounts.py`) is checked, sentence by sentence, against what the
model was actually shown this turn:

* a listed price only counts for the item it belongs to (and, in a sentence naming no item, for the
  items this turn is about);
* concession wording beside any other figure fails, even if that figure is some item's price;
* the customer's own figure is never repeated beside an item name when it is below the listed price,
  never accepted (decision D1: code writes the "noted your offer" line, not the model), and an echo of
  a below-list figure poisons any agreement word anywhere in the reply;
* percentages and number-less discounts ("half price", "for free") beside an item fail;
* knowledge-base and plan prices never sit beside an item name, and KB sentences that name a catalog
  item are ignored (a stale KB price must not beat the catalog).

This module only judges; the regenerate-then-fall-back flow is `guard_flow.py`.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal

from app.offerings.amounts import Amount, extract_amounts, mask_non_money, normalise
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
        # another item that the sentence names only in part still counts when it brings words the
        # explicit ones do not ("Black sneaker - ..., Leather bag - ...")
        known = set().union(*(_name_tokens(i) for i in explicit))
        extra = [
            i
            for f, i, exact in scored
            if not exact and f >= 0.6 and (_name_tokens(i) & words) - known
        ]
        return loose, explicit + extra
    best = max(f for f, _, _ in scored)
    return loose, [i for f, i, _ in scored if f == best]


_DISCOUNT_WORDS = re.compile(
    r"half[\s-]?price|\bfor free\b|\bfree\b|free of charge|on the house|discount|\boff\b|reduc"
    r"|cheaper|lower (?:the )?price|cut (?:the )?price|price (?:drop|cut)|markdown|bargain"
    r"|knock(?:ed)?|get one free|buy one|no (?:extra )?(?:cost|charge)|\bon us\b",
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
    r"|fair enough|you got it|we accept|can do)\b",
    re.IGNORECASE,
)
_PURE_ECHO = re.compile(
    r"^\s*(?:and\s+)?you(?:'ve|\s+have)?\s+(?:offered|said|mentioned|asked\s+for|proposed|"
    r"suggested|quoted|stated)\b",
    re.IGNORECASE,
)
_SHARED = re.compile(r"\b(?:each|both|same|all)\b", re.IGNORECASE)
_QTY_BEFORE = re.compile(
    r"(?<!size )(?<!sizes )(?<!#)\b(?:[2-9]|[1-9]\d|two|three|four|five|six|seven|eight|nine|ten|both)"
    r"\s+(?:of\s+)?(?:the\s+)?$",
    re.IGNORECASE,
)


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


def _values(texts: Iterable[str], kind: str, small_ok: bool = False) -> set[Decimal]:
    return {a.value for t in texts for a in extract_amounts(t, small_ok=small_ok) if a.kind == kind}


def _has(values: Iterable, v: Decimal) -> bool:
    return any(v == x for x in values)


def _kb_values(kb_text: str, items: list[OfferingFacts], small_ok: bool) -> tuple[set, set]:
    """Amounts and percentages from KB sentences that do not name a catalog item."""
    names = [i.name for i in items]
    money: set[Decimal] = set()
    pct: set[Decimal] = set()
    for sentence in _SENTENCE.split(normalise(kb_text)):
        if _named_items(sentence, items)[0]:
            continue
        for a in extract_amounts(mask_non_money(sentence, names), small_ok=small_ok):
            (money if a.kind == "money" else pct).add(a.value)
    return money, pct


def _anchors(sentence: str, item: OfferingFacts) -> list[tuple[int, int]]:
    """Where the sentence mentions this item: its ref, its full name, or any of its name words."""
    spans = [m.span() for m in re.finditer(rf"#\s?{item.seq}\b", sentence)]
    if item.name:
        spans += [m.span() for m in re.finditer(re.escape(item.name), sentence, re.IGNORECASE)]
    tokens = _name_tokens(item)
    for m in re.finditer(r"[A-Za-z0-9]+", sentence):
        if singular(m.group().lower()) in tokens:
            spans.append(m.span())
    return sorted(spans)


def _items_for_amount(
    sentence: str, strict: list[OfferingFacts], amount: Amount
) -> list[OfferingFacts]:
    """The named item this amount belongs to: the nearest mention before it, else the first after."""
    if len(strict) < 2:
        return strict
    before: dict[int, list[OfferingFacts]] = {}
    after: dict[int, list[OfferingFacts]] = {}
    for item in strict:
        spans = _anchors(sentence, item)
        prior = [e for _, e in spans if e <= amount.start]
        if prior:
            before.setdefault(max(prior), []).append(item)
        later = [st for st, _ in spans if st >= amount.end]
        if later:
            after.setdefault(min(later), []).append(item)
    if before:
        return before[max(before)]
    if after:
        return after[min(after)]
    return strict


def _bundle(sentence: str, items: list[OfferingFacts]) -> bool:
    """A quantity right before the item name beside a single price reads as a bundle price."""
    for item in items:
        spans = _anchors(sentence, item)
        if spans and _QTY_BEFORE.search(sentence[max(0, spans[0][0] - 14) : spans[0][0]]):
            return True
    return False


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
    refs = frozenset(i.seq for i in items)
    all_prices = {Decimal(i.price) for i in items if i.price is not None}
    small_ok = any(p < 1000 for p in all_prices)
    customer_money = _values(customer_texts, "money", small_ok)
    customer_pct = _values(customer_texts, "percent")
    kb_money, kb_pct = _kb_values(kb_text, items, small_ok)
    tiers = {Decimal(t) for t in tier_amounts}
    matched = {Decimal(i.price) for i in items if i.seq in grounding.matched_seqs and i.price}
    turn_prices = matched or all_prices  # what a sentence naming no item may quote
    ceiling = max(turn_prices, default=None)
    currency = (grounding.currency or "NGN").upper()
    names = [i.name for i in items]

    # the customer is bargaining when their latest message names a figure below a price in play,
    # or the model recorded an offer
    current = customer_texts[-1] if customer_texts else ""
    offer = offer_in_play or (
        ceiling is not None and any(v < ceiling for v in _values([current], "money", small_ok))
    )

    violations: list[Violation] = []
    echoed_below = False
    carried: list[OfferingFacts] = []  # the item the previous named sentence was about
    sentences = [s.strip() for s in _SENTENCE.split(normalise(reply)) if s.strip()]
    for sentence in sentences:
        masked = mask_non_money(sentence, names)
        amounts = extract_amounts(masked, small_ok=small_ok, ref_seqs=refs)
        loose, strict = _named_items(sentence, items)
        if strict:
            carried = strict
        cue = bool(_CUES.search(sentence))
        if not grounding.strict:
            if (loose or amounts or offer or re.search(r"\d", sentence)) and _unnegated(
                _DISCOUNT_WORDS, sentence
            ):
                violations.append(Violation(sentence, "discount_wording"))
            if amounts and len(strict) > 1 and _SHARED.search(sentence):
                prices = {i.price for i in strict}
                if len(prices) > 1:
                    violations.append(Violation(sentence, "shared_price"))
            if offer and (_unnegated(_AGREE, sentence) or _unnegated(_CUES, sentence)):
                violations.append(Violation(sentence, "agreement_after_offer"))
        listed_here = {Decimal(i.price) for i in strict if i.price is not None}
        states_listed = any(_has(listed_here, a.value) for a in amounts)
        for a in amounts:
            reason = None
            if grounding.strict:
                reason = "strict"
            elif a.kind == "percent":
                # a customer's own percentage may be quoted back, never accepted (D1)
                ok = (
                    not loose
                    and not cue
                    and (
                        _has(kb_pct, a.value)
                        or (_has(customer_pct, a.value) and _pure_echo(sentence, a))
                    )
                )
                reason = None if ok else "percent"
            elif a.currency and a.currency != currency:
                reason = "currency"
            else:
                target = _items_for_amount(sentence, strict, a)
                listed = {Decimal(i.price) for i in target if i.price is not None}
                reason = _judge_money(
                    a,
                    sentence,
                    bool(target),
                    listed,
                    states_listed,
                    carried if not strict else [],
                    turn_prices,
                    cue,
                    customer_money,
                    kb_money,
                    tiers,
                )
                if (
                    reason is None
                    and target
                    and _has(listed, a.value)
                    and _bundle(sentence, target)
                ):
                    reason = "bundle_price"
                if reason == "echo":  # a pure "you offered X": allowed, but it poisons agreement
                    reason = None
                    if ceiling is not None and a.value < ceiling:
                        echoed_below = True
            if reason:
                violations.append(Violation(sentence, reason, [a.value]))

    if echoed_below and not offer and not grounding.strict:
        # an echoed below-list figure followed by agreement anywhere in the reply
        joined = " ".join(sentences)
        if _unnegated(_AGREE, joined) or _unnegated(_CUES, joined):
            violations.append(Violation(reply, "agreement_after_offer"))
    return GuardVerdict(ok=not violations, violations=violations)


def _judge_money(
    a: Amount,
    sentence: str,
    names_item: bool,
    listed: set,
    states_listed: bool,
    carried: list[OfferingFacts],
    turn_prices: set,
    cue: bool,
    customer_money: set,
    kb_money: set,
    tiers: set,
) -> str | None:
    v = a.value
    customer_ok = _has(customer_money, v)
    echoed = _echoed(sentence, a)
    if names_item:
        if _has(listed, v):
            return None  # the named item's own listed price, whatever the wording
        if customer_ok:
            if cue:
                return "concession"
            if not listed or v < max(listed):
                return (
                    "customer_figure_below_price"  # code, not the model, writes "noted your offer"
                )
            # above the list price: only as a budget beside the item's own price
            return None if echoed and states_listed else "unattributed_customer_amount"
        if cue:
            return "concession"
        if _has(kb_money, v):
            return "kb_beside_item"
        return "wrong_item_price"  # includes a plan price beside an item name
    if customer_ok:
        if cue:
            return "concession"
        if echoed and _pure_echo(sentence, a):
            return "echo"
        return "unattributed_customer_amount"
    if cue:
        return "concession"  # even at a listed price: "I can do ₦12,000 for you"
    if carried:  # a sentence right after one naming an item is still about that item
        carried_prices = {Decimal(i.price) for i in carried if i.price is not None}
        if _has(carried_prices, v):
            return None
        return None if _has(tiers, v) or _has(kb_money, v) else "wrong_item_price"
    if _has(turn_prices, v) or _has(tiers, v) or _has(kb_money, v):
        return None
    return "not_in_block"
