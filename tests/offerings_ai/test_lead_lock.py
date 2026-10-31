"""T6 review (c): two parallel turns on one lead must queue, not double the wanted items."""

from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.db.models.lead import Lead
from app.db.models.tenant import Tenant
from app.offerings.orchestrate import lock_lead
from tests.offerings_ai.helpers import make_tenant


def test_lock_lead_holds_a_row_lock_until_the_transaction_ends(pg_engine):
    if pg_engine.dialect.name != "postgresql":
        pytest.skip("row locks need Postgres")
    first, second = Session(pg_engine), Session(pg_engine)
    tenant_id = make_tenant(first, sales_enabled=True)
    lead = Lead(
        tenant_id=tenant_id, conversation_id=uuid4(), intent="PURCHASE_INTENT", source="chat_widget"
    )
    first.add(lead)
    first.commit()
    try:
        lock_lead(first, lead)  # the first turn holds the lead
        with pytest.raises(OperationalError):  # the second turn would have to wait
            second.execute(select(Lead.id).where(Lead.id == lead.id).with_for_update(nowait=True))
        second.rollback()
        first.commit()  # released
        assert second.execute(
            select(Lead.id).where(Lead.id == lead.id).with_for_update(nowait=True)
        ).first()
    finally:
        first.rollback()
        second.rollback()
        first.execute(delete(Tenant).where(Tenant.id == tenant_id))
        first.commit()
        first.close()
        second.close()
