"""The sales prompt forbids the model from offering to email anything: only code can send."""

from app.sales.intent import build_sales_directive


def test_directive_tells_the_model_it_cannot_email_and_must_not_offer_to():
    d = build_sales_directive(["How big is your team?"]).lower()
    assert "cannot send email" in d
    assert "never offer, promise or ask to email" in d
    assert "the platform" in d and "tells the prospect" in d


def test_directive_still_lets_the_model_collect_an_email_address():
    d = build_sales_directive(["How big is your team?"]).lower()
    assert "name and email" in d
