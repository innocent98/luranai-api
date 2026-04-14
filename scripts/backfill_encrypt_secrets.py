#!/usr/bin/env python3
"""Backfill: encrypt existing plaintext ActionSecret.secret_value rows.

Idempotent — already-encrypted rows are detected by try_decrypt_field
and skipped (written back unchanged since EncryptedString re-encrypts
on every write, but the value is the same).

Usage:
    ENCRYPTION_KEY=<key> ENCRYPTION_MIGRATION_MODE=true \
    poetry run python scripts/backfill_encrypt_secrets.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.orm.attributes import flag_modified

from app.core.config import settings
from app.db.models.automation import ActionSecret


def main():
    engine = create_engine(settings.DATABASE_URL)
    Session = sessionmaker(bind=engine)
    session = Session()

    secrets = session.execute(select(ActionSecret)).scalars().all()
    count = 0

    for secret in secrets:
        # flag_modified forces SQLAlchemy to issue an UPDATE even though
        # the Python value is identical — this triggers EncryptedString's
        # process_bind_param to re-encrypt the value.
        flag_modified(secret, "secret_value")
        count += 1

    session.commit()
    session.close()
    print(f"Backfilled {count} action secret(s).")


if __name__ == "__main__":
    main()
