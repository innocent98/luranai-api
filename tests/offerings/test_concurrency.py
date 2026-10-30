"""Concurrent creates cannot overshoot the plan cap or hand out the same #seq (committed data)."""

import threading

from sqlalchemy.orm import sessionmaker

from app.db.models.offering import Offering
from app.db.models.tenant import Tenant
from app.services.offering_service import OfferingError, OfferingService
from tests.entitlements.helpers import make_tenant
from tests.offerings.helpers import add_items, body


def test_parallel_creates_respect_the_cap_and_never_share_a_seq(pg_engine):
    factory = sessionmaker(bind=pg_engine)
    setup = factory()
    tenant = make_tenant(setup, "trial")  # cap 25
    tenant_id = tenant.id
    add_items(setup, tenant, 20)
    setup.close()

    results: list[object] = []
    lock = threading.Lock()

    def worker():
        session = factory()
        try:
            created = OfferingService(session).create(tenant_id, body(name="Parallel"))
            outcome: object = created["seq"]
        except OfferingError as exc:
            outcome = exc.code
        finally:
            session.close()
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        seqs = sorted(r for r in results if isinstance(r, int))
        assert seqs == [21, 22, 23, 24, 25]  # exactly the five free slots, no duplicates
        assert results.count("offering_limit") == 5
    finally:
        cleanup = factory()
        cleanup.query(Offering).filter_by(tenant_id=tenant_id).delete()
        cleanup.query(Tenant).filter_by(id=tenant_id).delete()
        cleanup.commit()
        cleanup.close()
