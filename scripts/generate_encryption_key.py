#!/usr/bin/env python3
"""Generate a Fernet encryption key for ENCRYPTION_KEY env var."""

from cryptography.fernet import Fernet


def main():
    key = Fernet.generate_key().decode()
    print("Generated Fernet key (add to .env as ENCRYPTION_KEY):\n")
    print(f"ENCRYPTION_KEY={key}")
    print("\nTo rotate keys:")
    print("1. Set ENCRYPTION_KEY_PREVIOUS=<current key>")
    print("2. Set ENCRYPTION_KEY=<this new key>")
    print("3. Deploy and run the backfill script")
    print("4. Remove ENCRYPTION_KEY_PREVIOUS")


if __name__ == "__main__":
    main()
