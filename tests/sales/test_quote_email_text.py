"""Wording the visitor sees about the quote email: masked address, no model-made promises."""

import pytest

from app.sales.quote_email import (
    SentOutcome,
    finalize_reply,
    mask_email,
    strip_email_promises,
)


@pytest.mark.parametrize(
    "raw,masked",
    [
        ("alice@example.com", "a***@example.com"),
        ("A@Example.COM", "a***@example.com"),
        ("  bob.smith+tag@mail.co.uk ", "b***@mail.co.uk"),
    ],
)
def test_mask_keeps_one_character_and_the_domain(raw, masked):
    assert mask_email(raw) == masked


def test_mask_never_contains_the_local_part():
    assert "lice" not in mask_email("alice@example.com")


# What the model says that promises an email. Every one must be removed.
PROMISES = [
    "Would you like me to email it to you?",
    "Should I email it to you?",
    "Shall I email this quote to you?",
    "I'll email you the quote shortly.",
    "I will send the quote to your email.",
    "I can send the full proposal to your inbox if you like.",
    "Let me email you a copy of this quote.",
    "We'll email you the details right away.",
    "I've emailed the quote to you.",
    "I have sent the PDF to your email address.",
    "Do you want me to forward the quote by email?",
    "I'm happy to email you a summary.",
]

# Statements that are not a promise by the AI to email the quote. Every one must survive.
KEPT = [
    "What's your email address?",
    "Your total comes to ₦62,050 for 58 seats.",
    "To reset your password, we'll email you a reset link.",
    "Great, thanks Ada. I've noted your email for follow-up.",
    "Our team will reach out to you on the email you gave.",
    "You can reply to the quote you received by email from your account manager.",
]


@pytest.mark.parametrize("sentence", PROMISES)
def test_promises_are_removed(sentence):
    out = strip_email_promises(f"Here is your quote above. {sentence} Anything else?")
    assert out == "Here is your quote above. Anything else?"


@pytest.mark.parametrize("sentence", KEPT)
def test_ordinary_sentences_are_kept(sentence):
    text = f"Here is your quote above. {sentence} Anything else?"
    assert strip_email_promises(text) == text


def test_a_promise_that_also_asks_for_the_address_becomes_a_plain_request():
    out = strip_email_promises("Could you share your email so I can send you the quote?")
    assert out == "What's the best email address to reach you on?"


def test_a_reply_that_is_only_a_promise_is_not_left_empty():
    out = strip_email_promises("Should I email it to you?")
    assert out.strip() and "email" not in out.lower()


def test_multiline_replies_keep_their_line_breaks():
    text = "Plan: Growth\nTotal: ₦62,050\n\nShould I email it to you?"
    assert strip_email_promises(text) == "Plan: Growth\nTotal: ₦62,050"


def test_finalize_appends_the_masked_confirmation_and_never_the_full_address():
    out = finalize_reply(
        "Should I email it to you? Here it is.",
        SentOutcome.queued("a***@example.com"),
    )
    assert "I've sent the quote to a***@example.com" in out
    assert "alice@example.com" not in out
    assert out.count("email it to you") == 0


def test_finalize_with_a_cap_says_it_could_not_send_and_promises_nothing():
    out = finalize_reply("Here it is.", SentOutcome.capped())
    assert "couldn't email" in out
    assert "I'll" not in out and "I've sent" not in out


def test_finalize_without_an_outcome_only_cleans_the_reply():
    assert finalize_reply("Here it is.", None) == "Here it is."
    assert "email" not in finalize_reply("Here it is. Should I email it to you?", None).lower()


def test_invite_is_offered_only_when_asked_for():
    out = finalize_reply("Here is your quote.", SentOutcome.invite())
    assert "share your email address" in out
