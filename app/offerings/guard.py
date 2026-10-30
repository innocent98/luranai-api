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
        return loose, explicit
    best = max(f for f, _, _ in scored)
    return loose, [i for f, i, _ in scored if f == best]


_DISCOUNT_WORDS = re.compile(
    r"half[\s-]?price|\bfor free\b|free of charge|on the house|discount|\boff\b|reduc|cheaper"
    r"|lower (?:the )?price|cut (?:the )?price|price (?:drop|cut)|markdown|bargain|knock(?:ed)?",
    re.IGNORECASE,
)
_NEGATION = re.compile(
    r"\b(?:can't|cannot|can not|unable|not able|won't|will not|don't|do not|no discounts?|"
    r"not possible|never)\b",
    re.IGNORECASE,
)
_AGREE = re.compile(
    r"\b(?:yes|yeah|yep|okay|ok|sure|deal|agreed?|accept(?:ed)?|alright|perfect|good news|great news"
    r"|granted|approved|confirmed|fine|works?|no problem|of course|certainly|absolutely)\b",
    re.IGNORECASE,
)


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


def check_reply(
    reply: str,
    grounding: GroundingResult,
    *,
    customer_texts: Iterable[str] = (),
    kb_text: str = "",
    tier_amounts: Iterable[int] = (),
) -> GuardVerdict:
    customer_texts = list(customer_texts)
    items = grounding.items
    all_prices = {Decimal(i.price) for i in items if i.price is not None}
    small_ok = any(p < 1000 for p in all_prices)
    customer_money = _values(customer_texts, "money", small_ok)
    customer_pct = _values(customer_texts, "percent")
    kb_money, kb_pct = _kb_values(kb_text, items, small_ok)
    tiers = {Decimal(t) for t in tier_amounts}
    matched = {Decimal(i.price) for i in items if i.seq in grounding.matched_seqs and i.price}
    turn_prices = matched or all_prices  # what a sentence naming no item may quote
    currency = (grounding.currency or "NGN").upper()
    names = [i.name for i in items]

    violations: list[Violation] = []
    echoed_customer: list[Decimal] = []
    sentences = [s.strip() for s in _SENTENCE.split(normalise(reply)) if s.strip()]
    for sentence in sentences:
        masked = mask_non_money(sentence, names)
        amounts = extract_amounts(masked, small_ok=small_ok)
        loose, strict = _named_items(sentence, items)
        cue = bool(_CUES.search(sentence))
        if (
            not grounding.strict
            and (loose or amounts)
            and _DISCOUNT_WORDS.search(sentence)
            and not _NEGATION.search(sentence)
        ):
            violations.append(Violation(sentence, "discount_wording"))
        listed = {Decimal(i.price) for i in strict if i.price is not None}
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
                        or (_has(customer_pct, a.value) and _echoed(sentence, a))
                    )
                )
                reason = None if ok else "percent"
            elif a.currency and a.currency != currency:
                reason = "currency"
            else:
                reason = _judge_money(
                    a,
                    sentence,
                    bool(strict),
                    listed,
                    turn_prices,
                    cue,
                    customer_money,
                    kb_money,
                    tiers,
                    echoed_customer,
                )
            if reason:
                violations.append(Violation(sentence, reason, [a.value]))

    if echoed_customer and not grounding.strict:
        ceiling = max(turn_prices, default=None)
        if ceiling is not None and any(v < ceiling for v in echoed_customer):
            if _AGREE.search(" ".join(sentences)):
                violations.append(Violation(reply, "agreement_after_offer", echoed_customer))
    return GuardVerdict(ok=not violations, violations=violations)


def _judge_money(
    a: Amount,
    sentence: str,
    names_item: bool,
    listed: set,
    turn_prices: set,
    cue: bool,
    customer_money: set,
    kb_money: set,
    tiers: set,
    echoed_customer: list,
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
            if not listed or v < min(listed):
                return (
                    "customer_figure_below_price"  # code, not the model, writes "noted your offer"
                )
            return None if echoed and not cue else "unattributed_customer_amount"
        if cue:
            return "concession"
        if _has(kb_money, v):
            return "kb_beside_item"
        return "wrong_item_price"  # includes a plan price beside an item name
    if customer_ok:
        if cue:
            return "concession"
        if not echoed:
            return "unattributed_customer_amount"
        echoed_customer.append(v)
        return None
    if cue:
        return "concession"  # even at a listed price: "I can do ₦12,000 for you"
    if _has(turn_prices, v) or _has(tiers, v) or _has(kb_money, v):
        return None
    return "not_in_block"
