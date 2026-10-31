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

_PRICEY = r"(?:price|cost|amount|rate|fee|charge)"
BARGAIN = re.compile(
    r"\b(?:last\s+price|final\s+price|lowest\s+price|least\s+price|better\s+price|special\s+price"
    r"|(?:your|ur)\s+(?:best|least|last|lowest)(?=\s*(?:price|\?|$|please|pls|abeg|now|for\s+me))"
    r"|what(?:'s|\s+is)\s+the\s+best\s+price"
    r"|give\s+me\s+(?:a\s+|the\s+)?(?:best|last|least)(?:\s+price)?"
    r"|how\s+much\s+(?:is\s+)?(?:(?:your|the|ur)\s+)?(?:last|least|lowest|final)"
    r"|wetin\s+be\s+(?:the\s+|your\s+)?(?:last|least)|best\s+offer|elo\s+ni\s+(?:last|final|least)"
    r"|how\s+much\s+(?:will|can|would)\s+you\s+(?:sell|give|do|take)|give\s+me\s+for\s+\d{1,3}\b"
    r"|discount(?:s|ed)?|(?:make|do|give)\s+(?:it|me)\s+(?:a\s+bit\s+|little\s+)?cheaper|cheaper\s+price"
    r"|half[\s-]?price|bargain(?:ing)?|haggl\w*|negotiab\w+|negotiat\w+|better\s+deal|good\s+price"
    r"|knock(?:\s+\w+)?\s+off|lower\s+(?:the\s+|your\s+)?price|price\s+(?:drop|down|cut)"
    r"|drop\s+(?:the\s+|your\s+)?price|go\s+(?:a\s+bit\s+|little\s+|a\s+little\s+)?lower"
    r"|(?:you|u|una|abeg|price|it|am)\s+(?:fit\s+|go\s+|can\s+)?come\s+down"
    r"|come\s+down\s+(?:small|a\s+bit|a\s+little|on\s+(?:the\s+)?price|for\s+me)"
    r"|(?:can|could|will|would|pls|please|abeg|kindly)\s+(?:you\s+|u\s+)?reduce"
    r"(?!\s+(?:the\s+)?(?:volume|noise|size|weight|brightness|messages?|notifications?|speed|delivery|time"
    r"|sugar|fat|risk|stress|pain|swelling|spots?|marks?|wrinkles?|load|temperature|font|screen|audio"
    r"|sound|bass|lag|glare|waiting|queue|emails?|calls?|texts?))"
    r"|reduce\s+(?:am|it|the\s+price|your\s+price|price|for\s+me)|abeg\s+reduce|any\s+(?:small\s+)?reduction"
    r"|reduction\s+(?:in|on|of)\s+(?:the\s+)?price|(?:price|your)\s+reduction"
    rf"|{_PRICEY}[^.!?\n]{{0,20}}\b(?:too\s+(?:high|much|expensive|costly|steep)|reduce[ds]?)\b"
    r"|\b(?:it|am|dis|this|that)\s{0,3}(?:is\s{1,3}|'s\s{0,3}|e\s{1,3})?too\s+(?:high|much|expensive|costly|steep)\b"
    r"|too\s+(?:expensive|costly|steep|cost)|e\s+too\s+(?:much|cost)|e\s+(?:don\s+|dey\s+)?too\s+cost"
    r"|abeg\s+(?:last|small|do\s+something|consider|reduce)|consider\s+me|dash\s+me|last\s+last"
    r"|(?:you\s+)?agree(?:d)?\s+(?:to|with|on)\s+(?:my|the|this|that)\s+(?:price|offer|figure|amount)"
    r"|so\s+you\s+agree|accept\s+my|take\s+my\s+(?:offer|price)|my\s+(?:offer|price)\s+is"
    r"|any\s+(?:small\s+)?offers?|remove\s+(?:something|small)|(?:the\s+)?least\s+you(?:'ll|\s+will|\s+can)?"
    r"|price\s+for\s+(?:both|the\s+two|all(?:\s+of\s+them)?)|is\s+the\s+price\s+final|best\s+price|final\s*\?"
    r"|rock[\s-]?bottom|wiggle\s+room|bottom\s+price|floor\s+price|be\s+fair|cheapest\s+you"
    r"|(?:your|the)\s+lowest(?=\s*(?:price|\?|$|you|u\b|for|please|pls))"
    r"|what(?:'s|\s+is)\s+the\s+(?:lowest|cheapest|least|minimum)"
    r"|e\s+go\s+work|go\s+work\s+abi|can\s+we\s+agree|will\s+you\s+accept|would\s+you\s+consider"
    r"|my\s+limit|na\s+my\s+own|that'?s\s+my\s+limit|room\s+to\s+move"
    r"|can\s+we\s+talk|(?:do|give)\s+something\s+(?:for\s+me|on)|something\s+for\s+me\s+on|package\s+price"
    r"|bundle\s+price|(?:give\s+me\s+)?your\s+best(?:\s+on|\s+for)?|(?:a\s+)?bit\s+(?:much|high|expensive|pricey)"
    r"|pricey|pricy|costly|be\s+(?:a\s+bit\s+)?cheaper|price\s+(?:on|of|for)\s+(?:the\s+)?[\w\s]{1,20}\s+(?:fixed|negotiable|flexible)"
    r"|oga\s+last\s*\?|^\W*last\s*\?"
    r"|din\s+owo|won\s+ju|owo\s+re\s+ga\s+ju|rage(?:\s+\w+)?\s+(?:kudi|farashi\w*|mani)|rage\s+\w+"
    r"|ya\s+yi\s+tsada|tsada|belata)\b"
    r"|\b\d[\d.]{0,6}\s{0,2}(?:%|percent|per\s+cent)\s{0,2}(?:off|discount|less|reduction|cheaper)"
    r"(?!\s{0,2}(?:code|coupon|voucher|promo))"
    r"|\b(?:give|take|knock)\s{1,3}(?:me\s{1,3})?\d[\d.]{0,6}\s{0,2}(?:%|percent|per\s+cent)",
    re.IGNORECASE,
)
# offers written as a bare 2-3 digit number after an offer cue: "na 850 I get", "drop am to 900",
# "do am for 930?" (thousands, when the listed prices run to the hundred-thousands)
CUE_NUMBER = re.compile(
    r"\b(?:drop(?:\s+am)?\s+to|na|collect|fit\s+be|can\s+it\s+be|could\s+it\s+be|should\s+be|be|do\s+(?:am|it|the\s+[a-z0-9]+(?:\s+[a-z0-9]+)?)\s+for"
    r"|can\s+you\s+do\s+(?:the\s+[a-z0-9]+\s+)?for|(?:i\s+)?(?:fit\s+|go\s+|can\s+)?pay"
    r"|sell(?:\s+(?:it|am))?\s+for|make\s+(?:it|am|e\s+be)|for|at|abeg)\s+[₦#N]?(\d{2,3})\b"
    r"(?!\s{0,2}(?:%|k\b|m\b|pm\b|am\b|pcs|pieces|units|people|persons|guests|staff|members|customers|rooms"
    r"|seats|plates|portions|days|hours|mins|minutes|gb|tb|mb|inch|cm|kg|years|months|weeks|items|orders"
    r"|times|sizes?))",
    re.IGNORECASE,
)
# "850 for the iPhone", "700 for Galaxy S23": a bare number before "for <item>"
CUE_NUMBER_ITEM = re.compile(
    r"\b(\d{2,3})\s{1,2}for\s{1,2}(?:the\s{1,2})?(?P<w>[a-z0-9]+)", re.IGNORECASE
)
# "send me your account number, let me pay 950000": paying, not bargaining
PAY_NOW = re.compile(
    r"\b(?:account\s+number|send\s+(?:me\s+)?(?:your\s+)?account|payment\s+link|i\s+have\s+(?:paid|sent)"
    r"|transfer(?:red)?\s+(?:done|now)|let\s+me\s+pay|ready\s+to\s+pay\s+the\s+full)\b",
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
    r"\b(?:what\s+(?:can|could|do|are|is|will)|which|any|anything|something|show|list|options|under"
    r"|within|budget|below|up\s+to|recommend|suggest)\b",
    re.IGNORECASE,
)


# "is it ₦25,000 for each?", "does that cost 25k?": checking a price, not offering one
QUOTING = re.compile(
    r"\b(?:is|are|does|do|did)\s{1,3}(?:it|that|this|these|they|the\s+\w+(?:\s+\w+)?)\b[^?\n]{0,20}"
    r"(?:cost|price|₦|\bN\s?\d|\#\s?\d|\d)",
    re.IGNORECASE,
)


_OFFERISH = re.compile(
    r"\b(?:pay|offer|can|could|will|would|take|sell|give|let|abeg|ok|okay|possible|fit|go)\b",
    re.IGNORECASE,
)


# "could you do 10,500? I can pay now": an offer even beside a budget remark
OFFER_CUE = re.compile(
    r"\b(?:(?:can|could|will|would)\s+(?:you|u)\s+(?:do|take|sell|give|go|accept|collect)"
    r"|i\s+(?:can|fit|go|will)\s+pay|pay\s+(?:immediately|now|cash)|collect)\b",
    re.IGNORECASE,
)
# "make am 20k for both": a figure for a bundle is an offer even above one unit's price
BUNDLE_OFFER = re.compile(
    r"\d[\d,.]{0,10}\s{0,2}k?\b[^.?!\n]{0,14}\bfor\s+(?:both|the\s+two|all|two|2|three|3)\b"
    r"|\b(?:both|two|2|three|3|all)\b[^.?!\n]{0,14}\bfor\s+[₦#N]?\s?\d",
    re.IGNORECASE,
)
# "The price on your Instagram says 12,000, is that correct?": checking what they saw
SAW_PRICE = re.compile(
    r"\b(?:says|said|shows?|showed|listed|written|advertised|instagram|website|page|post|flyer)\b[^?\n]{0,40}"
    r"\d[\d,.]{0,10}k?[^?\n]{0,25}\?",
    re.IGNORECASE,
)


# "10 meat pies, 8000 total right?": confirming an order total
TOTAL_CHECK = re.compile(
    r"\d[\d,.]{0,10}k?\s{0,3}(?:total|altogether|in\s{1,3}all)\b[^?\n]{0,20}\?", re.IGNORECASE
)


def is_price_check(text: str) -> bool:
    """A real confirmation question: ends with `?`, asks whether a price is right, and says nothing
    that offers, pays or asks for a deal."""
    if SAW_PRICE.search(text) and not _OFFERISH.search(text):
        return True
    if TOTAL_CHECK.search(text) and not OFFER_CUE.search(text):
        return True
    return bool(QUOTING.search(text)) and "?" in text and not _OFFERISH.search(text)


def fold(text: str | None) -> str:
    """Accents folded away ("ẹ jọ ẹ din owo rẹ" reads "e jo e din owo re"), width folded too."""
    decomposed = unicodedata.normalize("NFKD", normalise((text or "")[:MAX_CHARS]))
    return "".join(c for c in decomposed if not unicodedata.combining(c)).replace("ọ", "o")


def has_bargain_wording(text: str | None) -> bool:
    return bool(BARGAIN.search(fold(text)))


def cap(texts) -> list[str]:
    """The customer messages worth analysing: the last few, each bounded."""
    return [(t or "")[:MAX_CHARS] for t in list(texts)[-MAX_TEXTS:]]
