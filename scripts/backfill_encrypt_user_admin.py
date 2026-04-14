#!/usr/bin/env python3
"""Backfill: encrypt User and PlatformAdmin email/full_name + populate email_hash.

Idempotent — safe to re-run. Uses flag_modified to force SQLAlchemy to
issue UPDATEs even when the Python value is unchanged, so EncryptedString's
process_bind_param re-encrypts on every run. The event listeners on the
models auto-populate email_hash from email.

Usage (locally or in container):
    ENCRYPTION_KEY=<key> ENCRYPTION_MIGRATION_MODE=true \
    python scripts/backfill_encrypt_user_admin.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.orm.attributes import flag_modified

from app.core.config import settings
from app.db.models.platform_admin import PlatformAdmin
from app.db.models.user import User


def main():
    engine = create_engine(settings.DATABASE_URL)
    Session = sessionmaker(bind=engine)
    session = Session()

    # Tenant staff users
    users = session.execute(select(User)).scalars().all()
    for u in users:
        # Event listener (_populate_user_email_hash) sets email_hash on update.
        flag_modified(u, "email")
        flag_modified(u, "full_name")
    session.commit()
    print(f"Backfilled {len(users)} tenant user(s).")

    # Platform admins
    admins = session.execute(select(PlatformAdmin)).scalars().all()
    for a in admins:
        flag_modified(a, "email")
        flag_modified(a, "full_name")
    session.commit()
    print(f"Backfilled {len(admins)} platform admin(s).")

    session.close()


if __name__ == "__main__":
    main()
