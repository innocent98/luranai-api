#!/usr/bin/env python3
"""Backfill: encrypt existing Message.content and Message.meta rows.

Usage:
    ENCRYPTION_KEY=<key> ENCRYPTION_MIGRATION_MODE=true \
    poetry run python scripts/backfill_encrypt_messages.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.db.models.conversation import Message


def main():
    engine = create_engine(settings.DATABASE_URL)
    Session = sessionmaker(bind=engine)
    session = Session()

    batch_size = 500
    offset = 0
    total = 0

    while True:
        messages = session.execute(
            select(Message).offset(offset).limit(batch_size)
        ).scalars().all()

        if not messages:
            break

        for msg in messages:
            msg.content = msg.content
            if msg.meta is not None:
                msg.meta = msg.meta
            total += 1

        session.commit()
        offset += batch_size
        print(f"  Processed {total} messages...")

    session.close()
    print(f"Backfilled {total} message(s).")


if __name__ == "__main__":
    main()
