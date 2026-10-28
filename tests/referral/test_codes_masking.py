"""Marketer code rules and the privacy mask applied to referred-tenant emails."""

import pytest

from app.services.referral.codes import validate_marketer_code
from app.services.referral.masking import mask_email


def test_code_rules():
    assert validate_marketer_code("abc") == "abc"
    assert validate_marketer_code("a" * 32) == "a" * 32
    assert validate_marketer_code("  Ada-Lagos-1 ") == "ada-lagos-1"
    for bad in ("ab", "a" * 33, "", "a_b", "a b", "ada!", "adä"):
        with pytest.raises(ValueError):
            validate_marketer_code(bad)


@pytest.mark.parametrize(
    "reserved", ["admin", "api", "login", "register", "luran", "support", "www", "ADMIN", "Luran"]
)
def test_reserved_codes_rejected(reserved):
    with pytest.raises(ValueError):
        validate_marketer_code(reserved)


def test_reserved_word_inside_a_longer_code_is_allowed():
    assert validate_marketer_code("admin-ada") == "admin-ada"


def test_mask_email():
    assert mask_email("ada@example.com") == "a***@e***.com"
    assert mask_email("x@y.co.ng") == "x***@y***.co.ng"
    assert "da" not in mask_email("ada@example.com").split("@")[0]
    assert "xample" not in mask_email("ada@example.com")


@pytest.mark.parametrize(
    "odd",
    ["", "   ", "no-at-sign", "a@b@c.com", "@example.com", "ada@", "@", "ada@.com", None, 5],
)
def test_mask_email_is_total_on_malformed_input(odd):
    assert mask_email(odd) == "***"


def test_mask_email_handles_unicode_and_dotless_domain():
    assert mask_email("émile@exämple.fr") == "é***@e***.fr"
    assert mask_email("a@localhost") == "a***@l***"
