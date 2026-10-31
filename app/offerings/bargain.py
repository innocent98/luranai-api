"""What counts as bargaining in a customer's words (shared by offer turns and the guard).

Two tiers (review passes 4 and 5): STRONG wording asks for a lower price whether or not a figure is
named ("last price", "e too cost", "the price is too high", "can you go lower"); a bare money figure
below the price in play is handled in `offer_turn`. Wording that is only a bargain beside a figure
("can you do", "how about", "make it") is not here: "can you do delivery to Lekki?" stays an
ordinary question. "reduce" and "too high" only count in a price context, so "does this cream
reduce dark spots?" and "is the heel too high for work?" are left to the model.

Yoruba, Hausa and Igbo phrases come from published usage, not from a native-speaker review.
"""

import re
import unicodedata

from app.offerings.amounts import normalise

MAX_TEXTS = 6  # customer messages analysed per turn
MAX_CHARS = 1500  # characters analysed per message

_PRICEY = r"(?:price|cost|amount|rate|fee|charge|it|am|this|that|dis)"
BARGAIN = re.compile(
    r"\b(?:last\s+price|final\s+price|lowest\s+price|least\s+price|better\s+price|special\s+price"
    r"|(?:your|ur)\s+(?:best|least|last|lowest)(?:\s+price)?|what(?:'s|\s+is)\s+the\s+best\s+price"
    r"|give\s+me\s+(?:a\s+|the\s+)?(?:best|last|least)(?:\s+price)?"
    r"|how\s+much\s+(?:is\s+)?(?:(?:your|the|ur)\s+)?(?:last|least|lowest|final)"
    r"|wetin\s+be\s+(?:the\s+|your\s+)?(?:last|least)|(?:the\s+)?last\s*\?"
    r"|discount(?:s|ed)?|(?:make|do|give)\s+(?:it|me)\s+(?:a\s+bit\s+|little\s+)?cheaper|cheaper\s+price"
    r"|half[\s-]?price|bargain(?:ing)?|haggl\w*|negotiab\w+|negotiat\w+|better\s+deal|good\s+price"
    r"|knock(?:\s+\w+)?\s+off|lower\s+(?:the\s+|your\s+)?price|price\s+(?:drop|down|cut)"
    r"|drop\s+(?:the\s+|your\s+)?price|come\s+down|go\s+(?:a\s+bit\s+|little\s+|a\s+little\s+)?lower"
    r"|(?:can|could|will|would|pls|please|abeg|kindly)\s+(?:you\s+|u\s+)?reduce"
    r"|reduce\s+(?:am|it|the\s+price|your\s+price|price|for\s+me)|abeg\s+reduce|any\s+(?:small\s+)?reduction"
    r"|reduction\s+(?:in|on|of)|(?:is\s+there\s+)?(?:any\s+)?reduction\??"
    rf"|{_PRICEY}[^.!?\n]{{0,20}}\b(?:too\s+(?:high|much|expensive|costly|steep)|reduce[ds]?)\b"
    r"|too\s+(?:expensive|costly|steep|cost)|e\s+too\s+(?:much|cost)|e\s+(?:don\s+|dey\s+)?too\s+cost"
    r"|abeg\s+(?:last|small|do\s+something|make|come)|dash\s+me|last\s+last|any\s+(?:small\s+)?offers?"
    r"|din\s+owo|won\s+ju|owo\s+re\s+ga\s+ju|rage(?:\s+\w+)?\s+(?:kudi|farashi\w*|mani)|rage\s+\w+"
    r"|ya\s+yi\s+tsada|tsada|belata)\b"
    r"|\b\d[\d.]{0,6}\s{0,2}(?:%|percent|per\s+cent)\s{0,2}(?:off|discount|less|reduction|cheaper)"
    r"|\b(?:give|take|knock)\s{1,3}(?:me\s{1,3})?\d[\d.]{0,6}\s{0,2}(?:%|percent|per\s+cent)",
    re.IGNORECASE,
)
# paying in parts is a payment arrangement, not a lower price: a figure beside it is not an offer
PAYMENT_PLAN = re.compile(
    r"\b(?:balance|instal+ments?|deposit|part[\s-]?payment|the\s+rest|remaining|on\s+delivery|upon\s+delivery"
    r"|pay\s+(?:half|part|some)\s+now|pay\s+small\s+small|layaway|pay\s+later)\b",
    re.IGNORECASE,
)
# "what can I get for 20k?": a budget question, not an offer on something
BROWSE = re.compile(
    r"\?|\b(?:what|which|any|anything|something|show|list|options|under|within|budget|below|up\s+to"
    r"|recommend|suggest)\b",
    re.IGNORECASE,
)


def fold(text: str | None) -> str:
    """Accents folded away ("ẹ jọ ẹ din owo rẹ" reads "e jo e din owo re"), width folded too."""
    decomposed = unicodedata.normalize("NFKD", normalise((text or "")[:MAX_CHARS]))
    return "".join(c for c in decomposed if not unicodedata.combining(c)).replace("ọ", "o")


def has_bargain_wording(text: str | None) -> bool:
    return bool(BARGAIN.search(fold(text)))


def cap(texts) -> list[str]:
    """The customer messages worth analysing: the last few, each bounded."""
    return [(t or "")[:MAX_CHARS] for t in list(texts)[-MAX_TEXTS:]]
