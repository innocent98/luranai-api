#!/usr/bin/env python3
"""Backfill: encrypt PII and populate hash columns.

Usage:
    ENCRYPTION_KEY=<key> ENCRYPTION_MIGRATION_MODE=true \
    poetry run python scripts/backfill_encrypt_pii.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.orm.attributes import flag_modified

from app.core.config import settings
from app.core.encryption import hash_for_lookup
from app.db.models.customer_user import CustomerUser
from app.db.models.visitor import Visitor, VisitorSession


def main():
    engine = create_engine(settings.DATABASE_URL)
    Session = sessionmaker(bind=engine)
    session = Session()

    customers = session.execute(select(CustomerUser)).scalars().all()
    for c in customers:
        # Compute hash from current plaintext value BEFORE flagging the column
        # for re-encryption (after flag_modified, reads still return plaintext
        # because the in-memory value hasn't been re-encrypted yet).
        c.email_hash = hash_for_lookup(c.email)
        flag_modified(c, "email")
        flag_modified(c, "full_name")
    session.commit()
    print(f"Backfilled {len(customers)} customer user(s).")

    visitors = session.execute(select(Visitor)).scalars().all()
    for v in visitors:
        if v.name:
            flag_modified(v, "name")
        if v.email:
            v.email_hash = hash_for_lookup(v.email)
            flag_modified(v, "email")
        if v.phone:
            v.phone_hash = hash_for_lookup(v.phone)
            flag_modified(v, "phone")
    session.commit()
    print(f"Backfilled {len(visitors)} visitor(s).")

    batch_size = 500
    offset = 0
    total = 0
    while True:
        sessions = session.execute(
            select(VisitorSession).offset(offset).limit(batch_size)
        ).scalars().all()
        if not sessions:
            break
        for s in sessions:
            if s.ip_address:
                s.ip_hash = hash_for_lookup(s.ip_address)
                flag_modified(s, "ip_address")
            if s.user_agent:
                flag_modified(s, "user_agent")
            total += 1
        session.commit()
        offset += batch_size
    print(f"Backfilled {total} visitor session(s).")

    session.close()


if __name__ == "__main__":
    main()
