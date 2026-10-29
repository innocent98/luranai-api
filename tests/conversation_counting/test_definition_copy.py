"""The words that define a "conversation" are identical everywhere users or tenants read them."""

from pathlib import Path

import pytest

from app.core.config import settings
from app.services import conversation_counting as cc

ROOT = Path(__file__).resolve().parents[2]

# The owner's wording (2026-10-29), written out by hand so a change to the code constant cannot
# silently rewrite the expectation.
CANONICAL = (
    "A conversation is one continuous exchange between a customer and your AI or team, from their "
    "first message until they have been quiet for 6 hours. It is not counted per message: one "
    "conversation can hold any number of messages, images and replies. If the same customer comes "
    "back within 6 hours it is still the same conversation; after 6 hours it is a new one. A "
    "conversation handed to a human agent stays one conversation until it is resolved. "
    "Conversations are counted per calendar month (UTC) and the count resets on the 1st. WhatsApp "
    "follows the same rule per chat."
)

COPIES = [
    "luran-knowledge-docs/subscription_plans_and_pricing.txt",
    "luran-knowledge-docs/LURAN_FAQ.md",
    "luran-knowledge-docs/luran_platform_knowledge.txt",
    "app/templates/emails/trial_expiring.html",
    "docs/SUBSCRIPTION_PLANS.md",
    "docs/SUBSCRIPTION_PRICING_MODEL.md",
    "docs/superpowers/specs/2026-10-27-pricing-and-entitlements-design.md",
    "docs/fe-integration-guide-entitlements.md",
    "docs/sop/conversation_limit_numbers.md",
]


def test_the_window_setting_is_the_six_hours_the_copy_says():
    """If you changed CONVERSATION_REUSE_WINDOW_HOURS, update CANONICAL here and every file in
    COPIES (and the frontends), then this test again."""
    assert settings.CONVERSATION_REUSE_WINDOW_HOURS == 6


def test_the_code_definition_is_the_canonical_wording():
    assert cc.conversation_definition() == CANONICAL


@pytest.mark.parametrize("path", COPIES)
def test_each_copy_holds_the_canonical_wording(path):
    assert CANONICAL in (ROOT / path).read_text(encoding="utf-8"), path


def test_the_faq_has_an_explicit_entry():
    faq = (ROOT / "luran-knowledge-docs/LURAN_FAQ.md").read_text(encoding="utf-8")
    assert "What counts as a conversation?" in faq
