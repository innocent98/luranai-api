"""Chunker behaviour for NEW ingestions (existing stored chunks are never rewritten)."""

from app.utils.chunking import text_chunker

BAKERY_FAQ = """Opening hours
We are open Monday to Saturday from 7am to 6pm, and Sundays from 8am to 2pm.

Delivery
We deliver within 10km of the shop. Delivery costs 1,500 naira and takes about 45 minutes.

Prices
A loaf of bread is 1,200 naira. Birthday cakes start at 15,000 naira.

Returns
If your order is wrong, tell us within 24 hours and we will replace it or refund you."""


def _contents(text):
    return [c["content"] for c in text_chunker.chunk_text(text)]


def test_short_document_becomes_one_chunk():
    chunks = _contents("We open at 7am every day.")
    assert chunks == ["We open at 7am every day."]


def test_text_just_at_the_minimum_is_kept():
    text = "x" * 20
    assert _contents(text) == [text]


def test_empty_and_whitespace_make_no_chunks():
    assert _contents("") == []
    assert _contents("   \n\n  ") == []


def test_multi_topic_faq_is_split_per_topic():
    chunks = _contents(BAKERY_FAQ)
    assert len(chunks) >= 4
    hours = [c for c in chunks if "7am" in c]
    assert len(hours) == 1
    # topical: the hours chunk does not also carry delivery or price details
    assert "naira" not in hours[0]


def test_question_and_answer_stay_together():
    text = (
        "Q: What time do you open?\nA: We open at 7am from Monday to Saturday.\n\n"
        "Q: Do you deliver?\nA: Yes, within 10km of the shop for 1,500 naira."
    )
    chunks = _contents(text)
    assert len(chunks) == 2
    assert "What time do you open?" in chunks[0] and "7am" in chunks[0]
    assert "Do you deliver?" in chunks[1] and "10km" in chunks[1]


def test_tiny_paragraphs_are_merged_not_dropped():
    text = "Open daily.\n\nCash only.\n\nNo parking.\n\nWifi is free."
    chunks = _contents(text)
    assert len(chunks) == 1
    for fragment in ("Open daily.", "Cash only.", "No parking.", "Wifi is free."):
        assert fragment in chunks[0]


def test_trailing_text_is_not_dropped():
    # The old chunker silently dropped a final chunk under 60 tokens.
    body = " ".join(f"Sentence number {i} talks about product details." for i in range(80))
    tail = "Final note: refunds take five days."
    chunks = _contents(body + " " + tail)
    assert any("refunds take five days" in c for c in chunks)


def test_long_paragraph_is_still_split_by_size():
    para = " ".join(f"Sentence number {i} talks about product details." for i in range(120))
    chunks = _contents(para)
    assert len(chunks) > 1
    assert all(c for c in chunks)


def test_large_document_of_short_paragraphs_does_not_explode_the_chunk_count():
    """Review finding: every 12+ token paragraph used to become its own chunk (embedding call)."""
    paragraphs = [
        f"Item {i}: the shop sells product number {i} for {i} naira." for i in range(2000)
    ]
    chunks = _contents("\n\n".join(paragraphs))
    assert len(chunks) < 400
    assert "product number 1999" in chunks[-1]  # nothing dropped
