"""Tests for updated security utilities."""

from unittest.mock import MagicMock


class TestCreateAccessToken:
    def test_includes_jti_claim(self):
        from app.core.security import create_access_token
        from jose import jwt
        from app.core.config import settings

        token = create_access_token(subject="user-123")
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        assert "jti" in payload
        assert len(payload["jti"]) > 10

    def test_jti_unique_per_token(self):
        from app.core.security import create_access_token
        from jose import jwt
        from app.core.config import settings

        t1 = create_access_token(subject="user-123")
        t2 = create_access_token(subject="user-123")
        p1 = jwt.decode(t1, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        p2 = jwt.decode(t2, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        assert p1["jti"] != p2["jti"]


class TestCreateRefreshToken:
    def test_creates_token_and_hash(self):
        from app.core.security import create_refresh_token

        token, token_hash = create_refresh_token()
        assert isinstance(token, str)
        assert len(token) > 20
        assert isinstance(token_hash, str)
        assert len(token_hash) == 64

    def test_hash_is_deterministic_for_same_token(self):
        import hashlib
        from app.core.security import create_refresh_token

        token, token_hash = create_refresh_token()
        expected = hashlib.sha256(token.encode()).hexdigest()
        assert token_hash == expected


class TestCookieHelpers:
    def test_set_auth_cookies(self):
        from app.core.security import set_auth_cookies

        response = MagicMock()
        set_auth_cookies(response, "access-tok", "refresh-tok")

        assert response.set_cookie.call_count == 2
        calls = response.set_cookie.call_args_list

        access_call = calls[0]
        assert access_call.kwargs["key"] == "access_token"
        assert access_call.kwargs["value"] == "access-tok"
        assert access_call.kwargs["httponly"] is True
        assert access_call.kwargs["secure"] is True
        assert access_call.kwargs["samesite"] == "lax"

        refresh_call = calls[1]
        assert refresh_call.kwargs["key"] == "refresh_token"
        assert refresh_call.kwargs["value"] == "refresh-tok"
        assert refresh_call.kwargs["httponly"] is True
        assert "/auth/refresh" in refresh_call.kwargs["path"]

    def test_clear_auth_cookies(self):
        from app.core.security import clear_auth_cookies

        response = MagicMock()
        clear_auth_cookies(response)

        assert response.delete_cookie.call_count == 2
