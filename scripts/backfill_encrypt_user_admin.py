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
from sqlalchemy.orm import load_only, sessionmaker
from sqlalchemy.orm.attributes import flag_modified

from app.core.config import settings
from app.db.models.platform_admin import PlatformAdmin
from app.db.models.user import User


def main():
    engine = create_engine(settings.DATABASE_URL)
    Session = sessionmaker(bind=engine)
    session = Session()

    # Tenant staff users.
    # load_only pins the query to just the columns this backfill touches, so it
    # stays valid when run at an INTERMEDIATE migration checkpoint in CD (before
    # `alembic upgrade head`). A bare `select(User)` compiles against the current
    # model and would SELECT columns added by later migrations (e.g. ac30's
    # `availability_status`) that don't exist yet at that checkpoint. Do NOT
    # revert this to `select(User)`.
    users = (
        session.execute(
            select(User).options(load_only(User.email, User.full_name, User.email_hash))
        )
        .scalars()
        .all()
    )
    for u in users:
        # Event listener (_populate_user_email_hash) sets email_hash on update.
        flag_modified(u, "email")
        flag_modified(u, "full_name")
    session.commit()
    print(f"Backfilled {len(users)} tenant user(s).")

    # Platform admins (same checkpoint-robustness guard as above).
    admins = (
        session.execute(
            select(PlatformAdmin).options(
                load_only(
                    PlatformAdmin.email,
                    PlatformAdmin.full_name,
                    PlatformAdmin.email_hash,
                )
            )
        )
        .scalars()
        .all()
    )
    for a in admins:
        flag_modified(a, "email")
        flag_modified(a, "full_name")
    session.commit()
    print(f"Backfilled {len(admins)} platform admin(s).")

    session.close()


if __name__ == "__main__":
    main()
