"""T15 on real Postgres: the unique key, savepoint-safe duplicates, cascades and the full drop path.

The default suite runs on in-memory SQLite; this is the dialect production uses.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import func, select, text

from app.channels import identity
from app.db.models.conversation import ConversationSession, Message
from app.db.models.whatsapp import (
    WhatsAppChat,
    WhatsAppContactExclusion,
    WhatsAppInboundMessage,
)
from app.services.whatsapp_exclusion import is_excluded
from app.services.whatsapp_exclusion_service import WhatsAppExclusionService
from app.services.whatsapp_inbound import handle_event
from app.services.whatsapp_service import WhatsAppError
from tests.whatsapp.conftest import connect, inbound, make_tenant, owner_typed

MUM = "2348012345678"


@pytest.fixture
def pg(db_session, fake_sidecar, queue):
    tenant = make_tenant(db_session)
    conn = connect(db_session, fake_sidecar, tenant)
    return SimpleNamespace(db=db_session, tenant=tenant, conn=conn)


def test_duplicate_add_keeps_the_session_usable(pg):
    svc = WhatsAppExclusionService(pg.db)
    first = svc.add(pg.tenant.id, pg.conn.id, f"+{MUM}", "Mum")
    with pytest.raises(WhatsAppError) as dup:
        svc.add(pg.tenant.id, pg.conn.id, MUM, None)
    assert dup.value.code == "already_excluded"
    assert svc.list(pg.tenant.id, pg.conn.id, 10, 0)[1] == 1  # the savepoint rolled back cleanly
    assert first.label == "Mum"


def test_bulk_with_existing_rows_counts_them_and_adds_the_rest(pg):
    svc = WhatsAppExclusionService(pg.db)
    svc.add(pg.tenant.id, pg.conn.id, MUM, None)
    res = svc.add_bulk(
        pg.tenant.id, pg.conn.id, [(MUM, None), ("2348022222222", "Dad"), ("0801", None)]
    )
    assert (res.added, res.already_excluded, res.rejected) == (
        1,
        1,
        [{"index": 2, "code": "invalid_phone"}],
    )


def test_unique_constraint_is_per_connection_and_secrets_are_sealed(pg, fake_sidecar):
    other = connect(pg.db, fake_sidecar, pg.tenant, "2348000000009")
    svc = WhatsAppExclusionService(pg.db)
    svc.add(pg.tenant.id, pg.conn.id, MUM, "Mum")
    svc.add(pg.tenant.id, other.id, MUM, None)  # same person on the other number is fine
    row = pg.db.execute(
        text("SELECT phone_masked, label FROM whatsapp_contact_exclusions WHERE label IS NOT NULL")
    ).one()
    assert "5678" not in row.phone_masked and "Mum" not in row.label  # Fernet at rest


def test_deleting_the_connection_cascades_the_list(pg):
    WhatsAppExclusionService(pg.db).add(pg.tenant.id, pg.conn.id, MUM, None)
    pg.db.execute(text("DELETE FROM whatsapp_connections WHERE id = :i"), {"i": pg.conn.id})
    assert pg.db.scalar(select(func.count()).select_from(WhatsAppContactExclusion)) == 0


@pytest.mark.asyncio
async def test_excluded_contact_leaves_no_trace_and_learns_its_lid(pg, llm, queue):
    db, conn = pg.db, pg.conn
    WhatsAppExclusionService(db).add(pg.tenant.id, conn.id, MUM, None)

    def counts():
        return [
            db.scalar(select(func.count()).select_from(m))
            for m in (WhatsAppChat, ConversationSession, Message, WhatsAppInboundMessage)
        ]

    before = counts()
    # first sighting: a LID with resolved digits -> dropped, LID learned
    assert (
        await handle_event(db, inbound(conn, ref="777@lid", from_phone_digits=MUM))
        == "dropped_excluded"
    )
    # later: LID only (no digits) and the owner typing to it -> still dropped
    assert await handle_event(db, inbound(conn, ref="777@lid")) == "dropped_excluded"
    assert await handle_event(db, owner_typed(conn, ref="777@lid")) == "dropped_excluded"
    assert counts() == before and llm.calls == 0 and queue.sends == []
    learned = db.execute(select(WhatsAppContactExclusion.lid_hmac)).scalar_one()
    assert learned == identity.contact_hmac("777@lid")
    # a stranger on the same number is answered
    assert await handle_event(db, inbound(conn, ref="2348033333333@s.whatsapp.net")) == "ai_reply"


@pytest.mark.asyncio
async def test_learning_removes_a_pause_row_the_owner_created_first(pg, llm):
    db, conn = pg.db, pg.conn
    WhatsAppExclusionService(db).add(pg.tenant.id, conn.id, MUM, None)
    # The owner typed to the LID before any digits were known: a pause-only row is created...
    db.add(
        WhatsAppChat(
            tenant_id=conn.tenant_id,
            connection_id=conn.id,
            contact_hmac=identity.contact_hmac("777@lid"),
            contact_ref="777@lid",
        )
    )
    db.commit()
    # ...and the first inbound that resolves the digits learns the LID and removes it.
    assert is_excluded(db, conn.id, ref="777@lid", phone_digits=MUM)
    assert db.scalar(select(func.count()).select_from(WhatsAppChat)) == 0
