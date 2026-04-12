#!/usr/bin/env python3
"""Backfill: encrypt authorization_code and migrate paystack_data to paystack_data_encrypted.

Idempotent — rows with paystack_data_encrypted already set are skipped.

Usage:
    ENCRYPTION_KEY=<key> ENCRYPTION_MIGRATION_MODE=true \
    poetry run python scripts/backfill_encrypt_payments.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.db.models.payment import PaymentTransaction, strip_paystack_data


def main():
    engine = create_engine(settings.DATABASE_URL)
    Session = sessionmaker(bind=engine)
    session = Session()

    txns = session.execute(select(PaymentTransaction)).scalars().all()
    migrated = 0

    for txn in txns:
        changed = False

        if txn.authorization_code:
            txn.authorization_code = txn.authorization_code
            changed = True

        if txn.paystack_data and not txn.paystack_data_encrypted:
            stripped = strip_paystack_data(txn.paystack_data)
            txn.paystack_data_encrypted = json.dumps(stripped)
            changed = True

        if changed:
            migrated += 1

    session.commit()
    session.close()
    print(f"Migrated {migrated} payment transaction(s).")


if __name__ == "__main__":
    main()
