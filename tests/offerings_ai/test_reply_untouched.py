"""A reply that holds no price token is never changed, for any tenant (review passes 4 and 5).

For a tenant with no active catalog items the whole catalog path is skipped: the reply is not
scanned, folded or stripped. For a tenant with items only exact tokens (and tokens disguised in
brackets on both sides) are ever removed; prose such as `price: #5,000` stays as written.
"""

from app.offerings import markup, tokens
from app.offerings.turn import finalize_reply, strip_token_text
from tests.offerings_ai.helpers import make_tenant
from tests.offerings_ai.test_chat_guard import ScriptedPlain
from tests.offerings_ai.test_guard import _grounding
from tests.offerings_ai.test_tokens_flow import _ask

PLAIN = [
    "Price: #5,000 per bag.",
    "Delivery price: #2500 within Lagos.",
    "Gold plan (price: 5000 naira/month)",
    "The price: ₦5,000, no hidden fees.",
    "price: #1",
    "Our price: 5000.",
    "Plan A (price: 12,000/month) or Plan B (price: 20,000/month).",
    "It costs #5,000 only.",
    "Total: #15,000\nDelivery: #2,000",
    "See [[Getting Started]] for the setup guide.",
    "Footnote [[1]] and [[2]] are below.",
    "Matrix: [[1, 2], [3, 4]]",
    '{"price": "#5000", "items": [[1, 2], [3, 4]]}',
    "```python\nprint([[1, 2], [3, 4]])\nprice = 5000\n```",
    "👨‍👩‍👧‍👦 Family pack, 🏳️‍🌈 pride edition, 👩🏽‍💻 dev kit.",
    "‏مرحبا، السعر 5000 نيرة‏",
    "Visit [our shop](https://example.com/shop?price=5000) today.",
    "| Item | Price |\n|------|-------|\n| Bag | #5,000 |\n| Shoe | #8,500 |",
    "Hello!\n\nHow can I help?\n\n1. Orders\n2. Delivery\n\nReply with a number.",
    "**Hours**\n- Mon-Fri  9am to 5pm\n- Sat   10am to 2pm",
    "Step one:   open Settings.\n\n\nStep two:\ttap Billing.",
    "Café menu: crème brûlée, naïve ½ price Tuesdays (ﬁnal).",
    "Call 0803 123 4567 or email hello@example.com.",
    "Use code PRICE10 at checkout (valid till 31/12/2026).",
    "The price is right! price:",
    "price:#abc and (price: later) are not numbers.",
    "Items [price list] attached; (see price) below.",
    "{price} and [price] and <price> are placeholders.",
    "Rated 4.5/5 [[source: reviews]] by 120 customers.",
    "ｆｕｌｌｗｉｄｔｈ ｔｅｘｔ, Ⅷ roman, ① circled, x² squared.",
    "A  B   C    D (several   spaces) and a tab\there.",
    "Line one\r\nLine two\r\nLine three",
    "Emoji: 😀😃😄 and ❤️ and 🇳🇬 flag.",
    "Zero​width‍space and soft­hyphen are fine.",
    'She said: "price: #5,000" and left.',
    "Question: what's the price: #? Answer: it depends.",
    "Price list:\n#1 Bag - #5,000\n#2 Shoe - #8,500",
    "Bank: GTB 0123456789 (price confirmation: #5000 deposit).",
    "Tip: type [[help]] anytime to see this menu again.",
    "x = [[0] * 3 for _ in range(3)]",
    "<b>price: #5000</b> <i>[[note]]</i>",
    "Prices start from #2,500 (price: from #2,500).",
    "Thanks!!! 🙏🙏   See you soon.   ",
    "   leading and trailing spaces   ",
    "",
    "Okay.",
    "A very long reply. " * 200,
    'Mixed: price: #5,000, [[wiki]], {"a": [[1]]}, ｆｕｌｌ, 👨‍👩‍👧',
    "Σ price: 5000 ≈ ₦5,000 ≠ #6,000",
    "Hi 👋 price: #3,000 only — today!",
    "Use [[brackets]] like this: [[a]], [[b]], [[c]].",
    "Cost (price: ₦4,000) vs (price: #4,000).",
    "(price: 1) (price: 2) (price: 3)",
    "[price: x] [price: y]",
    "Total price: #12,500.\nDelivery price: #1,500.\nGrand total: #14,000.",
    "price:#5000",
    "PRICE: #5000 / Price : #5000 / price :#5000",
    "Gold (price: #5000) Silver (price: #3000)",
    "Receipt no. #00123, paid by transfer.",
    'Reply "price: #" followed by the item number.',
]


def test_the_corpus_has_sixty_replies():
    assert len(PLAIN) >= 60


def test_a_tenant_without_a_catalog_gets_every_reply_back_byte_identical():
    for text in PLAIN:
        assert finalize_reply(None, None, None, text) == (text, [])


def test_the_catalog_code_is_not_even_called_for_them(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("catalog code ran for a tenant without a catalog")

    monkeypatch.setattr(tokens, "has_markup", boom)
    monkeypatch.setattr(markup, "has_price_markup", boom)
    monkeypatch.setattr(markup, "strip_price_markup", boom)
    assert finalize_reply(None, None, None, "Price: #5,000 [[price:#1]]") == (
        "Price: #5,000 [[price:#1]]",
        [],
    )


def test_a_catalog_tenant_keeps_prose_as_written_and_loses_only_exact_tokens():
    g = _grounding()
    for text in PLAIN:
        if "[[" in text or "]]" in text or markup.LOOKALIKE.search(markup.fold(text)):
            continue  # double brackets and `(price: 5)` look like a broken token in a catalog reply
        assert finalize_reply(None, None, g, text) == (text, []), text
        assert strip_token_text(text) == text, text


def test_prompt_text_keeps_kb_prose():
    kb = "Delivery price: #2,500 within Lagos. Gold plan (price: 5000 naira/month)."
    assert strip_token_text(kb) == kb
    assert strip_token_text("Hello [[price:#4]] and [price:#4] bye") == "Hello  and  bye"


def test_web_replies_of_a_tenant_without_a_catalog_are_untouched_end_to_end(client, db):
    tid = make_tenant(db)
    for text in PLAIN[:6]:
        r = _ask(client, tid, ScriptedPlain(text), text="what do you offer?")
        assert r.json()["message"]["content"] == text


def test_markup_scans_stay_linear_on_adversarial_text():
    import time

    nasty = [
        "[[" + " " * 10000 + " price",
        "[[" * 5000 + "price",
        "price" * 3000,
        "[[p" + "​" * 5000 + "rice:#1]]",
        "[[price: " * 2000,
        "[price:" + "1" * 5000,
    ]
    start = time.perf_counter()
    for text in nasty * 3:
        markup.strip_price_markup(text)
        tokens.has_stray_markup(text)
        tokens.strip(text)
    assert time.perf_counter() - start < 1.0


def test_no_catalog_helper_runs_for_a_tenant_without_items_end_to_end(client, db, monkeypatch):
    """The rule: nothing from the catalog code runs for them (gate aside)."""
    import app.api.v1.endpoints.chat as chat

    def boom(*a, **k):
        raise AssertionError("catalog code ran for a tenant without items")

    for name in (
        "offer_watch",
        "kb_text_from_prompt",
        "next_offer_watch",
        "arms_countdown",
        "context_prices",
        "is_offer_turn",
        "finalize_reply_markup",
        "strip_token_text",
        "guard_reply",
        "lead_source",
    ):
        if hasattr(chat, name):
            monkeypatch.setattr(chat, name, boom)
    tid = make_tenant(db)
    for text in PLAIN[:3]:
        r = _ask(client, tid, ScriptedPlain(text), text="what do you offer?")
        assert r.json()["message"]["content"] == text
