"""T15 contact exclusion: normalisation, CRUD API, and that a listed contact leaves no trace."""

from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text

from app.channels import identity, signing
from app.db.models.conversation import ConversationSession, Message
from app.db.models.visitor import Visitor
from app.db.models.whatsapp import (
    WhatsAppChat,
    WhatsAppContactExclusion,
    WhatsAppInboundMessage,
)
from app.services.whatsapp_exclusion import (
    digits_from_ref,
    is_excluded,
    normalize_exclusion_phone,
    phone_jid,
)
from app.services.whatsapp_inbound import handle_event
from app.services.whatsapp_outbound import send_text
from app.utils.datetime import utcnow_aware
from tests.whatsapp.conftest import (
    PHONE_A,
    PHONE_B,
    WEBHOOK_SECRET,
    connect,
    inbound,
    login_admin,
    make_tenant,
    owner_typed,
)

MUM = "2348012345678"
URL = "/api/v1/whatsapp/connections/{}/exclusions"


def _snap(row):
    """The `client` fixture closes the session after each request, which detaches ORM objects."""
    return SimpleNamespace(**{k: getattr(row, k) for k in ("id", "tenant_id", "session_key")})


@pytest.fixture
def tenant(db):
    return make_tenant(db)


@pytest.fixture
def conn(db, fake_sidecar, queue, tenant):
    return _snap(connect(db, fake_sidecar, tenant))


def _add(client, conn, phone=MUM, label=None):
    body = {"phone_number": phone}
    if label is not None:
        body["label"] = label
    return client.post(URL.format(conn.id), json=body)


def _counts(db):
    db.expire_all()
    return {
        "chats": db.scalar(select(func.count()).select_from(WhatsAppChat)),
        "sessions": db.scalar(select(func.count()).select_from(ConversationSession)),
        "messages": db.scalar(select(func.count()).select_from(Message)),
        "visitors": db.scalar(select(func.count()).select_from(Visitor)),
        "ledger": db.scalar(select(func.count()).select_from(WhatsAppInboundMessage)),
    }


def _exclude(db, conn, digits=MUM, lid_ref=None):
    entry = WhatsAppContactExclusion(
        tenant_id=conn.tenant_id,
        connection_id=conn.id,
        phone_hmac=identity.phone_hmac(digits),
        lid_hmac=identity.contact_hmac(lid_ref) if lid_ref else None,
        phone_masked=identity.mask_phone(digits),
    )
    db.add(entry)
    db.commit()
    return SimpleNamespace(id=entry.id)


# ----------------------------------------------------------------------------- normalisation
@pytest.mark.parametrize(
    "raw, expected",
    [
        ("2348012345678", MUM),
        ("+234 801 234 5678", MUM),
        ("+234-801-234-5678", MUM),
        ("00234 801 234 5678", MUM),
        ("(234) 801.234.5678", MUM),
        ("  +2348012345678  ", MUM),
        ("+234 (0) 803 123 4567", "2348031234567"),  # "(0)" trunk prefix is dropped
        ("+44 (0)7700 900123", "447700900123"),
        ("+234 0803 123 4567", "2348031234567"),  # trunk 0 after a code that never has one
        ("2340803 123 4567", "2348031234567"),
        ("08012345678", None),  # national format: no country code to resolve
        ("+0801234567", None),
        ("12345", None),
        ("1" * 16, None),
        ("abc", None),
        ("", None),
        (None, None),
        ("120363025246125486@g.us", None),  # a group id is not a phone
        ("1234567890@lid", None),
    ],
)
def test_normalize_exclusion_phone(raw, expected):
    assert normalize_exclusion_phone(raw) == expected


def test_digits_from_ref_only_for_phone_jids():
    assert digits_from_ref(f"{MUM}@s.whatsapp.net") == MUM
    assert digits_from_ref("1234567890@lid") is None
    assert digits_from_ref(f"{MUM}@g.us") is None
    assert digits_from_ref(None) is None


# ----------------------------------------------------------------------------- API
def test_add_list_remove(client, db, wa_settings, conn, tenant):
    login_admin(tenant)
    r = _add(client, conn, "+234 801 234 5678", label="  Mum  ")
    assert r.status_code == 201, r.text
    body = r.json()
    assert set(body) == {"id", "phone_masked", "label", "created_at"}
    assert body["label"] == "Mum" and body["phone_masked"] == "+234••••••5678"
    assert MUM not in r.text

    listed = client.get(URL.format(conn.id))
    assert listed.status_code == 200
    assert listed.json()["total"] == 1 and listed.json()["items"] == [body]
    assert (listed.json()["limit"], listed.json()["offset"]) == (100, 0)

    assert client.delete(f"{URL.format(conn.id)}/{body['id']}").status_code == 204
    assert client.get(URL.format(conn.id)).json()["total"] == 0
    assert client.delete(f"{URL.format(conn.id)}/{body['id']}").status_code == 404


def test_full_number_is_not_stored_anywhere(client, db, wa_settings, conn, tenant):
    login_admin(tenant)
    assert _add(client, conn, MUM, label="Mum").status_code == 201
    row = db.execute(text("SELECT * FROM whatsapp_contact_exclusions")).mappings().one()
    stored = " ".join(str(v) for v in row.values())
    assert MUM not in stored and "Mum" not in stored and "5678" not in stored  # masked is sealed
    assert row["phone_hmac"] == identity.phone_hmac(MUM) and row["lid_hmac"] is None


def test_duplicate_is_409_and_label_blank_is_none(client, db, wa_settings, conn, tenant):
    login_admin(tenant)
    assert _add(client, conn, MUM, label="   ").json()["label"] is None
    r = _add(client, conn, "+2348012345678")
    assert (r.status_code, r.json()["code"]) == (409, "already_excluded")


@pytest.mark.parametrize("bad", ["08012345678", "abc", "", "123"])
def test_invalid_phone_is_422(client, db, wa_settings, conn, tenant, bad):
    login_admin(tenant)
    r = _add(client, conn, bad)
    assert (r.status_code, r.json()["code"]) == (422, "invalid_phone")


def test_own_number_cannot_be_excluded(client, db, wa_settings, conn, tenant):
    login_admin(tenant)
    r = _add(client, conn, f"+{PHONE_A}")
    assert (r.status_code, r.json()["code"]) == (422, "own_number")


def test_lists_are_per_connection(client, db, wa_settings, fake_sidecar, queue, tenant):
    first = _snap(connect(db, fake_sidecar, tenant, PHONE_A))
    second = _snap(connect(db, fake_sidecar, tenant, PHONE_B))
    login_admin(tenant)
    assert _add(client, first, MUM).status_code == 201
    assert _add(client, second, MUM).status_code == 201  # same person, other number: own entry
    assert client.get(URL.format(first.id)).json()["total"] == 1
    assert client.get(URL.format(second.id)).json()["total"] == 1


def test_other_tenants_connection_and_entry_are_404(client, db, wa_settings, fake_sidecar, queue):
    mine = make_tenant(db)
    theirs = make_tenant(db)
    their_conn = _snap(connect(db, fake_sidecar, theirs, PHONE_B))
    entry = _exclude(db, their_conn)
    login_admin(mine)
    for call in (
        lambda: client.get(URL.format(their_conn.id)),
        lambda: _add(client, their_conn),
        lambda: client.post(URL.format(their_conn.id) + "/bulk", json={"entries": []}),
        lambda: client.delete(f"{URL.format(their_conn.id)}/{entry.id}"),
    ):
        r = call()
        assert r.status_code in (404, 422) and r.status_code != 200
    assert client.get(URL.format(their_conn.id)).json()["code"] == "not_found"
    db.expire_all()
    assert db.get(WhatsAppContactExclusion, entry.id) is not None  # untouched


def test_delete_with_wrong_connection_is_404(client, db, wa_settings, fake_sidecar, queue, tenant):
    one = _snap(connect(db, fake_sidecar, tenant, PHONE_A))
    two = _snap(connect(db, fake_sidecar, tenant, PHONE_B))
    entry = _exclude(db, one)
    login_admin(tenant)
    assert client.delete(f"{URL.format(two.id)}/{entry.id}").status_code == 404
    assert client.delete(f"{URL.format(one.id)}/{uuid4()}").status_code == 404


def test_add_is_gated_by_plan_and_kill_switch_but_list_and_remove_are_not(
    client, db, wa_settings, fake_sidecar, queue, monkeypatch
):
    no_plan = make_tenant(db, can_use=False)
    conn = _snap(connect(db, fake_sidecar, no_plan))
    entry = _exclude(db, conn)
    login_admin(no_plan)
    r = _add(client, conn)
    assert (r.status_code, r.json()["code"]) == (403, "whatsapp_not_enabled")
    assert (
        client.post(
            URL.format(conn.id) + "/bulk", json={"entries": [{"phone_number": MUM}]}
        ).status_code
        == 403
    )
    assert client.get(URL.format(conn.id)).json()["total"] == 1
    assert client.delete(f"{URL.format(conn.id)}/{entry.id}").status_code == 204

    on = make_tenant(db)
    conn2 = _snap(connect(db, fake_sidecar, on, PHONE_B))
    login_admin(on)
    monkeypatch.setattr("app.services.whatsapp_service.settings.WHATSAPP_ENABLED", False)
    assert _add(client, conn2).status_code == 403


def test_requires_tenant_admin(client, db, wa_settings, conn):
    from app.main import app

    app.dependency_overrides.clear()
    assert client.get(URL.format(conn.id)).status_code in (401, 403)


def test_bulk_add_is_partial_and_reports_rows(client, db, wa_settings, conn, tenant):
    login_admin(tenant)
    assert _add(client, conn, "+2348099999999").status_code == 201
    r = client.post(
        URL.format(conn.id) + "/bulk",
        json={
            "entries": [
                {"phone_number": "+234 801 234 5678", "label": "Mum"},  # new
                {"phone_number": "2348099999999"},  # already listed
                {"phone_number": "2348012345678"},  # same number again in this request
                {"phone_number": "08012345678"},  # invalid
                {"phone_number": f"+{PHONE_A}"},  # own number
                {"phone_number": "2348077777777"},  # new
            ]
        },
    )
    assert r.status_code == 200, r.text
    assert r.json() == {
        "added": 2,
        "already_excluded": 2,
        "rejected": [{"index": 3, "code": "invalid_phone"}, {"index": 4, "code": "own_number"}],
    }
    assert client.get(URL.format(conn.id)).json()["total"] == 3


def test_bulk_size_limits(client, db, wa_settings, conn, tenant):
    login_admin(tenant)
    r = client.post(URL.format(conn.id) + "/bulk", json={"entries": []})
    assert (r.status_code, r.json()["code"]) == (422, "invalid_bulk")
    many = [{"phone_number": f"23480{i:08d}"} for i in range(201)]
    r = client.post(URL.format(conn.id) + "/bulk", json={"entries": many})
    assert (r.status_code, r.json()["code"]) == (422, "invalid_bulk")


def test_per_connection_cap(client, db, wa_settings, conn, tenant, monkeypatch):
    monkeypatch.setattr("app.services.whatsapp_exclusion_service.MAX_EXCLUSIONS_PER_CONNECTION", 2)
    login_admin(tenant)
    assert _add(client, conn, "2348011111111").status_code == 201
    assert _add(client, conn, "2348022222222").status_code == 201
    r = _add(client, conn, "2348033333333")
    assert (r.status_code, r.json()["code"]) == (409, "exclusion_limit")
    bulk = client.post(
        URL.format(conn.id) + "/bulk",
        json={"entries": [{"phone_number": "2348044444444"}, {"phone_number": "2348055555555"}]},
    )
    assert (bulk.status_code, bulk.json()["code"]) == (409, "exclusion_limit")
    assert client.get(URL.format(conn.id)).json()["total"] == 2  # a refused request adds nothing


def test_pagination(client, db, wa_settings, conn, tenant):
    login_admin(tenant)
    for i in range(3):
        _add(client, conn, f"23480{i}0000000")
    page = client.get(URL.format(conn.id) + "?limit=2&offset=1").json()
    assert page["total"] == 3 and len(page["items"]) == 2 and page["offset"] == 1
    assert client.get(URL.format(conn.id) + "?limit=0").status_code == 422


def test_adding_removes_a_pause_only_chat_row(client, db, wa_settings, conn, tenant):
    chat = WhatsAppChat(
        tenant_id=conn.tenant_id,
        connection_id=conn.id,
        contact_hmac=identity.contact_hmac(phone_jid(MUM)),
        contact_ref=phone_jid(MUM),
        human_until=utcnow_aware(),
    )
    db.add(chat)
    db.commit()
    login_admin(tenant)
    assert _add(client, conn, MUM).status_code == 201
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(WhatsAppChat)) == 0


def test_adding_keeps_an_existing_conversation(client, db, wa_settings, conn, tenant):
    session = ConversationSession(
        tenant_id=conn.tenant_id, user_identifier="wa_x", source="whatsapp"
    )
    db.add(session)
    db.flush()
    db.add(
        WhatsAppChat(
            tenant_id=conn.tenant_id,
            connection_id=conn.id,
            contact_hmac=identity.contact_hmac(phone_jid(MUM)),
            contact_ref=phone_jid(MUM),
            session_id=session.id,
        )
    )
    db.commit()
    login_admin(tenant)
    assert _add(client, conn, MUM).status_code == 201
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(WhatsAppChat)) == 1  # history is kept


# ----------------------------------------------------------------------------- matching
@pytest.mark.asyncio
async def test_match_by_phone_jid_declared_digits_and_learned_lid(db, conn):
    entry = _exclude(db, conn)
    entry = db.get(WhatsAppContactExclusion, entry.id)
    assert is_excluded(db, conn.id, ref=phone_jid(MUM))
    assert not is_excluded(db, conn.id, ref="999@lid")  # a LID alone says nothing yet
    assert is_excluded(db, conn.id, ref="999@lid", phone_digits=MUM)  # resolved digits match...
    db.expire_all()
    assert entry.lid_hmac == identity.contact_hmac("999@lid")  # ...and the LID is learned
    assert is_excluded(db, conn.id, ref="999@lid")  # so a later event without digits matches
    assert not is_excluded(db, conn.id, ref="888@lid")
    assert not is_excluded(db, conn.id, ref=phone_jid("2348011111111"))


@pytest.mark.asyncio
async def test_other_connections_list_does_not_apply(db, fake_sidecar, queue, tenant):
    one = connect(db, fake_sidecar, tenant, PHONE_A)
    two = connect(db, fake_sidecar, tenant, PHONE_B)
    _exclude(db, one)
    assert is_excluded(db, one.id, ref=phone_jid(MUM))
    assert not is_excluded(db, two.id, ref=phone_jid(MUM))


@pytest.mark.asyncio
async def test_learning_only_from_a_lid_ref_never_a_group_or_other_ref(db, conn):
    entry = db.get(WhatsAppContactExclusion, _exclude(db, conn).id)
    is_excluded(db, conn.id, ref="120363025246125486@g.us", phone_digits=MUM)
    db.expire_all()
    assert entry.lid_hmac is None


# ----------------------------------------------------------------------------- pipeline
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {"ref": f"{MUM}@s.whatsapp.net"},
        {"ref": "555@lid", "from_phone_digits": MUM},
    ],
)
async def test_excluded_text_leaves_no_trace_and_no_reply(db, conn, queue, llm, kwargs):
    _exclude(db, conn)
    before = _counts(db)
    out = await handle_event(db, inbound(conn, **kwargs))
    assert out == "dropped_excluded"
    assert _counts(db) == before and llm.calls == 0 and queue.sends == []


@pytest.mark.asyncio
async def test_excluded_image_other_media_and_replays_leave_no_trace(db, conn, queue, llm):
    _exclude(db, conn)
    before = _counts(db)
    ref = f"{MUM}@s.whatsapp.net"
    image = inbound(
        conn,
        ref=ref,
        kind="image",
        text="",
        media={"id": "m1", "mime": "image/png", "bytes": 10, "sha256": "0" * 64},
    )
    other = inbound(conn, ref=ref, kind="other", text="")
    for ev in (image, other, image):  # the replay is dropped too, with no ledger row
        assert await handle_event(db, ev) == "dropped_excluded"
    assert _counts(db) == before and queue.sends == [] and queue.images == []


@pytest.mark.asyncio
async def test_owner_typing_to_an_excluded_contact_creates_no_pause_row(db, conn, queue, llm):
    _exclude(db, conn, lid_ref="777@lid")
    before = _counts(db)
    assert await handle_event(db, owner_typed(conn, ref="777@lid")) == "dropped_excluded"
    assert (
        await handle_event(db, owner_typed(conn, ref=f"{MUM}@s.whatsapp.net")) == "dropped_excluded"
    )
    assert _counts(db) == before


@pytest.mark.asyncio
async def test_unlisted_contacts_still_get_the_ai(db, conn, queue, llm):
    _exclude(db, conn)
    out = await handle_event(db, inbound(conn, ref="2348011111111@s.whatsapp.net"))
    assert out == "ai_reply" and llm.calls == 1


@pytest.mark.asyncio
async def test_removing_the_entry_restores_normal_handling(
    client, db, wa_settings, conn, tenant, queue, llm
):
    login_admin(tenant)
    entry_id = _add(client, conn, MUM).json()["id"]
    ref = f"{MUM}@s.whatsapp.net"
    assert await handle_event(db, inbound(conn, ref=ref)) == "dropped_excluded"
    assert client.delete(f"{URL.format(conn.id)}/{entry_id}").status_code == 204
    assert await handle_event(db, inbound(conn, ref=ref)) == "ai_reply"


@pytest.mark.asyncio
async def test_lid_chat_that_already_exists_is_dropped_after_learning(db, conn, queue, llm):
    """The AI answered a LID contact before the number was listed; once an event resolves its
    digits, the chat's stored ref (LID) matches too and nothing more is sent to it."""
    await handle_event(db, inbound(conn, ref="321@lid"))
    assert len(queue.sends) == 1
    chat = db.execute(select(WhatsAppChat)).scalar_one()
    _exclude(db, conn)
    assert is_excluded(db, conn.id, ref="321@lid", phone_digits=MUM)  # learns the LID
    out = await send_text(db, conn.id, chat.id, "late AI reply", 1, guard=True)
    assert out == "dropped_excluded"
    out = await send_text(db, conn.id, chat.id, "agent words", 1)  # a human agent too
    assert out == "dropped_excluded"


@pytest.mark.asyncio
async def test_queued_reply_to_a_contact_listed_afterwards_is_dropped(
    db, conn, queue, llm, fake_sidecar
):
    ref = f"{MUM}@s.whatsapp.net"
    await handle_event(db, inbound(conn, ref=ref))
    chat = db.execute(select(WhatsAppChat)).scalar_one()
    sent = queue.sends[0]
    _exclude(db, conn)
    out = await send_text(db, conn.id, chat.id, sent["text"], 1, guard=sent["guard"])
    assert out == "dropped_excluded"
    assert fake_sidecar.sent == [] if hasattr(fake_sidecar, "sent") else True


@pytest.mark.asyncio
async def test_agent_takeover_conversation_gets_nothing_once_excluded(db, conn, queue, llm):
    from app.db.models.conversation import SessionStatus

    ref = f"{MUM}@s.whatsapp.net"
    await handle_event(db, inbound(conn, ref=ref))
    chat = db.execute(select(WhatsAppChat)).scalar_one()
    session = db.get(ConversationSession, chat.session_id)
    session.status = SessionStatus.ESCALATED
    session.agent_id = None
    db.commit()
    _exclude(db, conn)
    before = _counts(db)
    assert (
        await handle_event(db, inbound(conn, ref=ref, text="are you there?")) == "dropped_excluded"
    )
    assert _counts(db) == before and llm.calls == 1  # no new message, AI never resumed
    assert await send_text(db, conn.id, chat.id, "Agent: hello", 1) == "dropped_excluded"


# ----------------------------------------------------------------------------- webhook
def _post(client, event):
    raw = json.dumps(event).encode()
    return client.post(
        "/api/v1/webhooks/whatsapp/linked",
        content=raw,
        headers={signing.SIGNATURE_HEADER: signing.sign(WEBHOOK_SECRET, raw)},
    )


def test_webhook_never_queues_an_excluded_contacts_event(client, db, wa_settings, conn, queue):
    _exclude(db, conn, lid_ref="777@lid")
    for ev in (
        inbound(conn, ref=f"{MUM}@s.whatsapp.net"),
        inbound(conn, ref="777@lid", from_phone_digits=MUM),
        inbound(conn, ref="777@lid"),
        owner_typed(conn, ref="777@lid"),
    ):
        r = _post(client, ev)
        assert (r.status_code, r.json()) == (200, {"status": "ignored"}), ev
    assert queue.events == []
    ok = _post(client, inbound(conn, ref="2348011111111@s.whatsapp.net"))
    assert ok.json() == {"status": "accepted"} and len(queue.events) == 1


# ----------------------------------------------------------------------------- purge
def test_account_purge_removes_the_list(db, wa_settings, conn, tenant):
    from app.services.account_purge_service import AccountPurgeService

    _exclude(db, conn)
    AccountPurgeService(db).purge_tenant(tenant.id)
    assert db.scalar(select(func.count()).select_from(WhatsAppContactExclusion)) == 0


# ----------------------------------------------------------------------------- already-seen LID chat
@pytest.mark.asyncio
async def test_contact_seen_by_lid_then_excluded_is_recognised_without_new_digits(
    client, db, wa_settings, conn, tenant, queue, llm
):
    """The AI already talks to 321@lid (the sidecar resolved the number on that message). The
    tenant then lists the number: the very next event, even with no digits, is dropped."""
    out = await handle_event(db, inbound(conn, ref="321@lid", from_phone_digits=MUM))
    assert out == "ai_reply"
    chat = db.execute(select(WhatsAppChat)).scalar_one()
    assert chat.phone_hmac == identity.phone_hmac(MUM)
    chat_id = chat.id
    login_admin(tenant)
    assert _add(client, conn, MUM).status_code == 201
    db.expire_all()
    entry = db.execute(select(WhatsAppContactExclusion)).scalar_one()
    assert entry.lid_hmac == identity.contact_hmac("321@lid")  # linked at add time
    assert db.get(WhatsAppChat, chat_id) is not None  # its conversation is history: kept
    before = _counts(db)
    assert await handle_event(db, inbound(conn, ref="321@lid")) == "dropped_excluded"
    assert await handle_event(db, owner_typed(conn, ref="321@lid")) == "dropped_excluded"
    assert _counts(db) == before
    for guard in (True, False):  # AI reply and agent / system message
        assert await send_text(db, conn.id, chat_id, "x", 1, guard=guard) == "dropped_excluded"


@pytest.mark.asyncio
async def test_lid_chat_without_a_remembered_number_is_matched_through_its_chat_key(
    db, conn, queue, llm
):
    """Chat seen by LID with no digits, later an event resolves them: the chat remembers the key,
    and an entry added afterwards (directly, as the service would) still matches by ref only."""
    await handle_event(db, inbound(conn, ref="654@lid"))
    chat = db.execute(select(WhatsAppChat)).scalar_one()
    assert chat.phone_hmac is None
    chat.phone_hmac = identity.phone_hmac(MUM)  # what a later inbound with digits records
    db.commit()
    _exclude(db, conn)
    assert is_excluded(db, conn.id, ref="654@lid")


@pytest.mark.asyncio
async def test_missing_pepper_in_the_sender_is_a_retry_not_a_lost_job(
    db, conn, queue, llm, monkeypatch
):
    from app.services.whatsapp_outbound import RetrySend

    await handle_event(db, inbound(conn, ref="987@lid"))
    chat = db.execute(select(WhatsAppChat)).scalar_one()
    monkeypatch.setattr("app.channels.identity.settings.WHATSAPP_CONTACT_PEPPER", "")
    with pytest.raises(RetrySend):
        await send_text(db, conn.id, chat.id, "x", 1, guard=True)
