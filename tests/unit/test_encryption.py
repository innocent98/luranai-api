"""Tests for field-level encryption utilities."""

import importlib

import pytest
from cryptography.fernet import Fernet


@pytest.fixture(autouse=True)
def _set_encryption_key(monkeypatch):
    """Set a valid Fernet key for all tests."""
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("ENCRYPTION_KEY", key)
    monkeypatch.delenv("ENCRYPTION_KEY_PREVIOUS", raising=False)
    monkeypatch.delenv("ENCRYPTION_MIGRATION_MODE", raising=False)


class TestEncryptField:
    def test_encrypt_returns_different_from_plaintext(self):
        from app.core.encryption import encrypt_field

        plaintext = "hello world"
        ciphertext = encrypt_field(plaintext)
        assert ciphertext != plaintext

    def test_roundtrip(self):
        from app.core.encryption import decrypt_field, encrypt_field

        plaintext = "sensitive data 123"
        ciphertext = encrypt_field(plaintext)
        assert decrypt_field(ciphertext) == plaintext

    def test_produces_different_ciphertext_each_call(self):
        from app.core.encryption import encrypt_field

        plaintext = "same input"
        ct1 = encrypt_field(plaintext)
        ct2 = encrypt_field(plaintext)
        assert ct1 != ct2

    def test_invalid_token_raises(self):
        from app.core.encryption import decrypt_field

        with pytest.raises(Exception):  # noqa: B017
            decrypt_field("not-a-valid-fernet-token")


class TestTryDecryptField:
    def test_decrypts_encrypted(self):
        from app.core.encryption import encrypt_field, try_decrypt_field

        plaintext = "secret"
        ciphertext = encrypt_field(plaintext)
        assert try_decrypt_field(ciphertext) == plaintext

    def test_returns_plaintext_when_not_encrypted(self):
        from app.core.encryption import try_decrypt_field

        value = "just a plain string"
        assert try_decrypt_field(value) == value

    def test_returns_empty_string_as_is(self):
        from app.core.encryption import try_decrypt_field

        assert try_decrypt_field("") == ""


class TestHashForLookup:
    def test_deterministic(self):
        from app.core.encryption import hash_for_lookup

        assert hash_for_lookup("test@example.com") == hash_for_lookup("test@example.com")

    def test_different_input_different_hash(self):
        from app.core.encryption import hash_for_lookup

        assert hash_for_lookup("a@example.com") != hash_for_lookup("b@example.com")

    def test_returns_64_char_hex(self):
        from app.core.encryption import hash_for_lookup

        result = hash_for_lookup("anything")
        assert len(result) == 64
        assert all(c in "0123456789abcdef" for c in result)

    def test_case_sensitive(self):
        from app.core.encryption import hash_for_lookup

        assert hash_for_lookup("Hello") != hash_for_lookup("hello")


class TestKeyRotation:
    def test_decrypt_with_previous_key_after_rotation(self, monkeypatch):
        import app.core.encryption as enc_module

        # Generate two keys
        old_key = Fernet.generate_key().decode()
        new_key = Fernet.generate_key().decode()

        # Encrypt with the old key
        monkeypatch.setenv("ENCRYPTION_KEY", old_key)
        monkeypatch.delenv("ENCRYPTION_KEY_PREVIOUS", raising=False)
        importlib.reload(enc_module)
        ciphertext = enc_module.encrypt_field("rotate me")

        # Rotate: new key is current, old key is previous
        monkeypatch.setenv("ENCRYPTION_KEY", new_key)
        monkeypatch.setenv("ENCRYPTION_KEY_PREVIOUS", old_key)
        importlib.reload(enc_module)
        assert enc_module.decrypt_field(ciphertext) == "rotate me"
