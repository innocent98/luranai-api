"""Test that deps.py reads tokens from cookies and checks jti revocation."""

import pytest
from unittest.mock import patch, MagicMock


class TestExtractToken:
    def test_extracts_from_cookie(self):
        from app.api.deps import extract_token

        request = MagicMock()
        request.cookies = {"access_token": "cookie-token-value"}
        request.headers = {}

        assert extract_token(request) == "cookie-token-value"

    def test_extracts_from_authorization_header(self):
        from app.api.deps import extract_token

        request = MagicMock()
        request.cookies = {}
        request.headers = {"authorization": "Bearer header-token-value"}

        assert extract_token(request) == "header-token-value"

    def test_cookie_takes_precedence(self):
        from app.api.deps import extract_token

        request = MagicMock()
        request.cookies = {"access_token": "from-cookie"}
        request.headers = {"authorization": "Bearer from-header"}

        assert extract_token(request) == "from-cookie"

    def test_returns_none_when_no_token(self):
        from app.api.deps import extract_token

        request = MagicMock()
        request.cookies = {}
        request.headers = {}

        assert extract_token(request) is None


class TestJtiRevocation:
    def test_is_token_revoked_returns_false_for_valid(self):
        from app.api.deps import is_token_revoked

        with patch("app.api.deps._get_redis") as mock_redis:
            mock_redis.return_value.exists.return_value = 0
            assert is_token_revoked("some-jti") is False

    def test_is_token_revoked_returns_true_for_revoked(self):
        from app.api.deps import is_token_revoked

        with patch("app.api.deps._get_redis") as mock_redis:
            mock_redis.return_value.exists.return_value = 1
            assert is_token_revoked("some-jti") is True
