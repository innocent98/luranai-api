"""Field-level encryption utilities using Fernet symmetric encryption.

Provides transparent encrypt/decrypt for SQLAlchemy columns via the
EncryptedString TypeDecorator, plus deterministic hashing for lookups.

Key management:
    ENCRYPTION_KEY          — current Fernet key (all new writes)
    ENCRYPTION_KEY_PREVIOUS — optional old key (decrypt-only, for rotation)
    ENCRYPTION_MIGRATION_MODE — "true" to tolerate plaintext reads (dual-read)
"""

import hashlib
import os

from cryptography.fernet import Fernet, MultiFernet
from loguru import logger
from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator


def _build_fernet() -> MultiFernet:
    """Build a MultiFernet from environment keys."""
    current_key = os.environ.get("ENCRYPTION_KEY", "")
    previous_key = os.environ.get("ENCRYPTION_KEY_PREVIOUS", "")

    if not current_key:
        raise RuntimeError(
            "ENCRYPTION_KEY environment variable is required. "
            "Generate one with: python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'"
        )

    keys = [Fernet(current_key.encode())]
    if previous_key:
        keys.append(Fernet(previous_key.encode()))
    else:
        logger.warning(
            "ENCRYPTION_KEY_PREVIOUS not set. Key rotation requires setting "
            "the previous key to decrypt data encrypted under the old key."
        )

    return MultiFernet(keys)


def _is_migration_mode() -> bool:
    return os.environ.get("ENCRYPTION_MIGRATION_MODE", "false").lower() == "true"


def encrypt_field(plaintext: str) -> str:
    """Encrypt a string value. Returns a URL-safe base64 Fernet token."""
    f = _build_fernet()
    return f.encrypt(plaintext.encode()).decode()


def decrypt_field(ciphertext: str) -> str:
    """Decrypt a Fernet token back to plaintext. Raises on invalid token."""
    f = _build_fernet()
    return f.decrypt(ciphertext.encode()).decode()


def try_decrypt_field(value: str) -> str:
    """Attempt decryption; return value as-is if it's not a Fernet token."""
    if not value:
        return value
    try:
        return decrypt_field(value)
    except Exception:
        return value


def hash_for_lookup(value: str) -> str:
    """SHA-256 hex digest for deterministic lookups (email_hash, phone_hash)."""
    return hashlib.sha256(value.encode()).hexdigest()


class EncryptedString(TypeDecorator):
    """SQLAlchemy column type that transparently encrypts/decrypts string values."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return encrypt_field(value)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if _is_migration_mode():
            return try_decrypt_field(value)
        return decrypt_field(value)
