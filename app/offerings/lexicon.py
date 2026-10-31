"""The one place that says what a reply may not promise about price.

A reply is a **price promise** when a sentence pairs a commitment subject ("I'll", "we can", "let
me", "you'll get", "the price will") with a price-lowering or comparative phrase ("cheaper", "drop
the price", "better deal", "remove small"). The model never gets to word a negotiation: the
code-only offer turns do that. This is a stop-gap, not a proof: it is a data table with its own
test table (`tests/offerings_ai/test_lexicon.py`); extend it from the pilot's miss log, or put an
LLM judge behind it (see the SOP, v1.5 option).

Pidgin, Yoruba, Hausa and Igbo entries come from published usage and have NOT had a native-speaker
review. Matching runs on accent-folded, lower-cased text (`bargain.fold`).
"""

import re

# who is promising: first person, an offer to act, or the price itself "coming down"
COMMITMENT: list[tuple[str, str]] = [
    (
        "english",
        r"\b(?:i|we)\b(?:'ll|'d|\s+(?:will|can|could|would|may|might|shall|am\s+going\s+to|are\s+going\s+to))?",
    ),
    ("english", r"\blet\s+me\b|\blet\s+us\b|\blet's\b"),
    ("english", r"\byou(?:'ll|\s+will|\s+can|\s+could|\s+may|\s+should)\s+(?:get|have|pay|take)\b"),
    (
        "english",
        r"\b(?:the\s+)?(?:price|it|they)\s+(?:will|can|could|may|might|would)\s+(?:be|come|go|drop|get)\b",
    ),
    ("english", r"\bfor\s+you\b[^.!?]{0,20}\b(?:price|rate)\b"),
    (
        "pidgin",
        r"\b(?:i|we)\s+(?:go|fit|dey)\b|\bmake\s+(?:i|we)\b|\bna\s+me\s+go\b|\bno\s+wahala\b",
    ),
    ("yoruba", r"\bmo\s+(?:maa|ma|le|fe)\b|\ba\s+(?:maa|ma|le)\b"),
    ("hausa", r"\bzan\b|\bza\s*mu\b|\bzamu\b|\bzan\s+iya\b"),
    ("igbo", r"\baga\s+m\b|\bm\s+ga\b|\banyi\s+ga\b|\bm\s+nwere\s+ike\b"),
]

_OBJ = r"(?:it|am|that|this|them|the\s+price|your\s+price|price|a\s+(?:bit|little)|small|something)"

# what is promised: a lower price, a better price, a deal
LOWERING: list[tuple[str, str]] = [
    ("english", r"\bcheaper\b|\bcheap\s+for\s+you\b"),
    (
        "english",
        r"\bmore\s+affordable\b|\baffordable\s+for\s+you\b|\bpay\s+less\b|\bless\s+money\b",
    ),
    (
        "english",
        r"\b(?:be|get|become|go)\s+(?:a\s+bit\s+|a\s+little\s+|much\s+|slightly\s+)?(?:lower|less|reduced|smaller)\b",
    ),
    (
        "english",
        rf"\blow(?:er|ered|ering)\s+{_OBJ}\b|\blower\s+(?:price|rate|figure)\b|\bfor\s+less\b|\bat\s+less\b",
    ),
    (
        "english",
        rf"\bdrop(?:ped|ping)?\s+{_OBJ}\b|\bdrop\s+(?:the\s+)?(?:price|rate)\b|\bdrop\s+to\b",
    ),
    ("english", r"\bslash(?:ed|ing)?\b"),
    ("english", rf"\badjust(?:ed|ing)?\s+(?:{_OBJ}|for\s+you|to\s+you)\b"),
    ("english", r"\breduc(?:e|ed|es|ing|tion)\b"),
    (
        "english",
        rf"\bcut(?:ting)?\s+{_OBJ}\b|\btrim\b|\bshave\b|\bknock(?:ed|ing)?\b[^.!?]{{0,12}}\boff\b|\btake\s+(?:\w+\s+){{0,2}}off\b",
    ),
    ("english", r"\b(?:come|bring|go|brought)\s+(?:it\s+|the\s+price\s+)?down\b"),
    ("english", r"\bdiscount(?:s|ed)?\b|\bmark(?:ed)?\s*down\b|\bhalf[\s-]?price\b"),
    (
        "english",
        r"\b(?:better|special|friendly|good|best|nice|fair|reasonable|sweet|reduced|lower|discounted|cheaper|affordable|promo)\s+(?:price|rate|deal|offer|figure|amount)\b",
    ),
    (
        "english",
        r"\bwork\s+(?:something|it|things)\s+out\b|\bworth\s+your\s+while\b|\bmeet\s+you\b|\bsort\s+you\b|\bsort\s+it\s+out\b",
    ),
    (
        "english",
        r"\bconsider\s+you\b|\bconsider\s+(?:your\s+)?(?:price|offer|figure|amount)\b|\bdo\s+you\s+a\s+(?:good|favou?r)\b",
    ),
    (
        "english",
        r"\bremove\s+(?:small|some|something)\b|\bsmall\s+(?:something|dash)\b|\bdash\s+you\b",
    ),
    (
        "pidgin",
        r"\b(?:do|make)\s+am\s+cheaper\b|\breduce\s+am\b|\blower\s+am\b|\bdrop\s+am\b|\bcut\s+am\b|\bdash\s+(?:you|am)\b",
    ),
    (
        "pidgin",
        r"\bgive\s+you\s+(?:better|special|good|small)\b|\bsort\s+you\s+out\b|\bsort\s+you\s+small\b",
    ),
    ("yoruba", r"\bdin\s+owo\b|\bdin\s+(?:re|e)\b|\bsan\s+owo\b|\bowo\s+re\s+ku\b|\bfi\s+owo\b"),
    ("hausa", r"\brage\b|\bragewa\b|\brangwame\b|\bsauki\b[^.!?]{0,10}\bfarashi\b"),
    ("igbo", r"\bbelata\b|\bmbelata\b|\bebelata\b|\bebelatara\b"),
]

# agreement phrases that count as accepting a figure once the customer has bargained or named one
AGREEMENT: list[tuple[str, str]] = [
    (
        "english",
        r"\baccept(?:ed|s)?\b(?!\s+(?:bank|cash|card|transfers?|payments?|pos|visa|master\s?card|cheques?|crypto|ussd|naira|dollars?|orders?|returns?|exchanges?))",
    ),
    (
        "english",
        r"\bagree(?:d|s)?\b|\bdeal\b|\bsounds\s+(?:good|great|fair)\b|\bsettled\b|\bwe\s+have\s+a\s+deal\b",
    ),
    ("pidgin", r"\be\s+don\s+do\b|\bwe\s+agree\b|\bno\s+wahala\b[^.!?]{0,15}\bam\b"),
]

# acceptance of what the customer proposed, with or without bargaining nearby
AGREEMENT_ALWAYS: list[tuple[str, str]] = [
    (
        "english",
        r"\b(?:i|we)(?:'ll|\s+will|\s+can|\s+do|\s+hereby)?\s+(?:accept|agree)(?:\s+to)?\s+(?:that|your|it|this|the\s+(?:price|offer|figure|amount|deal)|my)\b",
    ),
    (
        "english",
        r"\bso\s+we(?:'re|\s+are)\s+agreed\b|\bwe(?:'re|\s+are)\s+agreed\b|\bagreed\s+then\b",
    ),
]
NEGATION = re.compile(
    r"\b(?:can't|cannot|can\s+not|unable\s+to|not\s+able\s+to|won't|will\s+not|don't|do\s+not|never"
    r"|no|not|without|isn't|aren't)\b",
    re.IGNORECASE,
)

_COMPILED_COMMIT = [(lang, re.compile(rx, re.IGNORECASE)) for lang, rx in COMMITMENT]
_COMPILED_LOWER = [(lang, re.compile(rx, re.IGNORECASE)) for lang, rx in LOWERING]
_COMPILED_AGREE = [(lang, re.compile(rx, re.IGNORECASE)) for lang, rx in AGREEMENT]
_COMPILED_AGREE_ALWAYS = [(lang, re.compile(rx, re.IGNORECASE)) for lang, rx in AGREEMENT_ALWAYS]


def commits(sentence: str) -> bool:
    return any(rx.search(sentence) for _lang, rx in _COMPILED_COMMIT)


def lowering_matches(sentence: str) -> list[re.Match]:
    return [m for _lang, rx in _COMPILED_LOWER for m in rx.finditer(sentence)]


def agreement_matches(sentence: str) -> list[re.Match]:
    return [m for _lang, rx in _COMPILED_AGREE for m in rx.finditer(sentence)]


def agreement_always_matches(sentence: str) -> list[re.Match]:
    return [m for _lang, rx in _COMPILED_AGREE_ALWAYS for m in rx.finditer(sentence)]
