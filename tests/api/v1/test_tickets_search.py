"""Tests for the optional ``q`` search param on GET /api/v1/tickets.

Semantics under test: case-insensitive literal substring match over
ticket_number, summary, ConversationSession.user_identifier and the linked
visitor's name / email / phone (all three are Fernet-encrypted at rest, so the
visitor fields are matched after decryption, not with SQL ILIKE). ANDed with the
other filters; tenant scoping is unchanged.
"""

import uuid

from app.db.models.conversation import (
    ConversationSession,
    SessionStatus,
    Ticket,
    TicketStatus,
)
from app.db.models.visitor import Visitor, VisitorSource
from app.utils.datetime import utcnow_naive


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _make_tenant_user(db_session):
    """Create a fresh Tenant + TENANT_ADMIN User; return (user, token)."""
    from app.core.security import create_access_token
    from app.db.models.tenant import Tenant
    from app.db.models.user import User, UserRole

    prefix = uuid.uuid4().hex[:8]
    tenant = Tenant(
        name=f"tenant_{prefix}",
        display_name=f"Tenant {prefix}",
        api_key_hash=uuid.uuid4().hex,
        subscription_tier="trial",
        subscription_status="trialing",
    )
    db_session.add(tenant)
    db_session.flush()

    user = User(
        email=f"user_{prefix}@example.com",
        hashed_password="$2b$12$placeholder_hash_not_real",
        full_name=f"User {prefix}",
        role=UserRole.TENANT_ADMIN,
        tenant_id=tenant.id,
        is_active=True,
    )
    db_session.add(user)
    db_session.flush()
    db_session.refresh(user)

    token = create_access_token(
        subject=str(user.id),
        additional_claims={"role": user.role.value, "tenant_id": str(user.tenant_id)},
    )
    return user, token


def _visitor(db_session, tenant_id, *, name=None, email=None, phone=None) -> Visitor:
    now = utcnow_naive()
    v = Visitor(
        tenant_id=tenant_id,
        public_token=f"tok-{uuid.uuid4().hex}",
        name=name,
        email=email,
        phone=phone,
        first_seen_at=now,
        last_seen_at=now,
        created_from=VisitorSource.WIDGET,
    )
    db_session.add(v)
    db_session.flush()
    return v


def _ticket(
    db_session,
    tenant_id,
    *,
    number: str,
    summary: str = "generic issue",
    visitor: Visitor | None = None,
    user_identifier: str | None = None,
    status: TicketStatus = TicketStatus.OPEN,
) -> Ticket:
    session = ConversationSession(
        tenant_id=tenant_id,
        user_identifier=user_identifier or f"anon-{uuid.uuid4().hex[:8]}",
        visitor_id=visitor.id if visitor else None,
        status=SessionStatus.ACTIVE,
    )
    db_session.add(session)
    db_session.flush()
    t = Ticket(
        tenant_id=tenant_id,
        session_id=session.id,
        ticket_number=number,
        summary=summary,
        status=status,
    )
    db_session.add(t)
    db_session.flush()
    return t


def _search(pg_client, token, q, **params):
    resp = pg_client.get("/api/v1/tickets", params={"q": q, **params}, headers=_auth(token))
    return resp


def _numbers(resp) -> set[str]:
    assert resp.status_code == 200, resp.text
    return {t["ticket_number"] for t in resp.json()["tickets"]}


def test_no_q_returns_everything(pg_client, db_session):
    user, token = _make_tenant_user(db_session)
    _ticket(db_session, user.tenant_id, number="TKT-A-1")
    _ticket(db_session, user.tenant_id, number="TKT-A-2")

    resp = pg_client.get("/api/v1/tickets", headers=_auth(token))

    assert _numbers(resp) == {"TKT-A-1", "TKT-A-2"}
    assert resp.json()["total"] == 2


def test_matches_ticket_number(pg_client, db_session):
    user, token = _make_tenant_user(db_session)
    _ticket(db_session, user.tenant_id, number="TKT-2026-0042")
    _ticket(db_session, user.tenant_id, number="TKT-2026-0099")

    resp = _search(pg_client, token, "0042")

    assert _numbers(resp) == {"TKT-2026-0042"}
    assert resp.json()["total"] == 1


def test_matches_summary(pg_client, db_session):
    user, token = _make_tenant_user(db_session)
    _ticket(db_session, user.tenant_id, number="TKT-S-1", summary="Refund for double charge")
    _ticket(db_session, user.tenant_id, number="TKT-S-2", summary="Password reset")

    assert _numbers(_search(pg_client, token, "double charge")) == {"TKT-S-1"}


def test_matches_user_identifier(pg_client, db_session):
    user, token = _make_tenant_user(db_session)
    _ticket(db_session, user.tenant_id, number="TKT-U-1", user_identifier="widget-user-777")
    _ticket(db_session, user.tenant_id, number="TKT-U-2", user_identifier="widget-user-888")

    assert _numbers(_search(pg_client, token, "user-777")) == {"TKT-U-1"}


def test_matches_visitor_name_email_phone(pg_client, db_session):
    user, token = _make_tenant_user(db_session)
    tid = user.tenant_id
    ada = _visitor(
        db_session, tid, name="Ada Lovelace", email="ada@example.test", phone="+234800111"
    )
    bob = _visitor(db_session, tid, name="Bob Stone", email="bob@other.test", phone="+234800222")
    _ticket(db_session, tid, number="TKT-V-ADA", visitor=ada)
    _ticket(db_session, tid, number="TKT-V-BOB", visitor=bob)

    assert _numbers(_search(pg_client, token, "lovel")) == {"TKT-V-ADA"}
    assert _numbers(_search(pg_client, token, "bob@other")) == {"TKT-V-BOB"}
    assert _numbers(_search(pg_client, token, "800222")) == {"TKT-V-BOB"}


def test_is_case_insensitive_everywhere(pg_client, db_session):
    user, token = _make_tenant_user(db_session)
    tid = user.tenant_id
    ada = _visitor(db_session, tid, name="Ada Lovelace", email="Ada@Example.Test")
    _ticket(db_session, tid, number="TKT-C-1", summary="Billing Problem", visitor=ada)
    _ticket(db_session, tid, number="TKT-C-2", summary="unrelated")

    assert _numbers(_search(pg_client, token, "tkt-c-1")) == {"TKT-C-1"}
    assert _numbers(_search(pg_client, token, "BILLING problem")) == {"TKT-C-1"}
    assert _numbers(_search(pg_client, token, "ADA LOVELACE")) == {"TKT-C-1"}
    assert _numbers(_search(pg_client, token, "ada@example.TEST")) == {"TKT-C-1"}


def test_query_is_stripped(pg_client, db_session):
    user, token = _make_tenant_user(db_session)
    _ticket(db_session, user.tenant_id, number="TKT-P-1", summary="alpha")
    _ticket(db_session, user.tenant_id, number="TKT-P-2", summary="beta")

    assert _numbers(_search(pg_client, token, "  alpha  ")) == {"TKT-P-1"}


def test_single_char_query_is_ignored(pg_client, db_session):
    user, token = _make_tenant_user(db_session)
    _ticket(db_session, user.tenant_id, number="TKT-I-1", summary="alpha")
    _ticket(db_session, user.tenant_id, number="TKT-I-2", summary="beta")

    resp = _search(pg_client, token, "z")

    assert _numbers(resp) == {"TKT-I-1", "TKT-I-2"}
    assert resp.json()["total"] == 2


def test_blank_and_whitespace_query_is_ignored(pg_client, db_session):
    user, token = _make_tenant_user(db_session)
    _ticket(db_session, user.tenant_id, number="TKT-B-1")

    assert _numbers(_search(pg_client, token, "")) == {"TKT-B-1"}
    assert _numbers(_search(pg_client, token, "   ")) == {"TKT-B-1"}
    assert _numbers(_search(pg_client, token, " a ")) == {"TKT-B-1"}


def test_query_over_100_chars_is_422(pg_client, db_session):
    _user, token = _make_tenant_user(db_session)

    assert _search(pg_client, token, "x" * 101).status_code == 422
    assert _search(pg_client, token, "x" * 100).status_code == 200


def test_percent_is_matched_literally(pg_client, db_session):
    user, token = _make_tenant_user(db_session)
    _ticket(db_session, user.tenant_id, number="TKT-L-1", summary="discount of 50% applied")
    _ticket(db_session, user.tenant_id, number="TKT-L-2", summary="discount of 500 applied")

    assert _numbers(_search(pg_client, token, "50%")) == {"TKT-L-1"}
    # a bare wildcard must not match everything
    assert _numbers(_search(pg_client, token, "%%")) == set()


def test_underscore_is_matched_literally(pg_client, db_session):
    user, token = _make_tenant_user(db_session)
    _ticket(db_session, user.tenant_id, number="TKT-M-1", summary="order a_b failed")
    _ticket(db_session, user.tenant_id, number="TKT-M-2", summary="order axb failed")

    assert _numbers(_search(pg_client, token, "a_b")) == {"TKT-M-1"}


def test_backslash_is_matched_literally(pg_client, db_session):
    user, token = _make_tenant_user(db_session)
    _ticket(db_session, user.tenant_id, number="TKT-K-1", summary=r"path C:\temp broke")
    _ticket(db_session, user.tenant_id, number="TKT-K-2", summary="path Ctemp broke")

    assert _numbers(_search(pg_client, token, "C:\\t")) == {"TKT-K-1"}


def test_literal_wildcards_in_visitor_fields(pg_client, db_session):
    user, token = _make_tenant_user(db_session)
    tid = user.tenant_id
    v1 = _visitor(db_session, tid, name="100% Joe")
    v2 = _visitor(db_session, tid, name="100 Joe")
    _ticket(db_session, tid, number="TKT-W-1", visitor=v1)
    _ticket(db_session, tid, number="TKT-W-2", visitor=v2)

    assert _numbers(_search(pg_client, token, "100%")) == {"TKT-W-1"}


def test_combines_with_status_filter(pg_client, db_session):
    user, token = _make_tenant_user(db_session)
    tid = user.tenant_id
    _ticket(db_session, tid, number="TKT-F-1", summary="refund", status=TicketStatus.OPEN)
    _ticket(db_session, tid, number="TKT-F-2", summary="refund", status=TicketStatus.RESOLVED)
    _ticket(db_session, tid, number="TKT-F-3", summary="other", status=TicketStatus.OPEN)

    resp = _search(pg_client, token, "refund", status_filter="open")

    assert _numbers(resp) == {"TKT-F-1"}
    assert resp.json()["total"] == 1


def test_visitor_match_respects_status_filter(pg_client, db_session):
    user, token = _make_tenant_user(db_session)
    tid = user.tenant_id
    ada = _visitor(db_session, tid, name="Ada Lovelace")
    _ticket(db_session, tid, number="TKT-G-1", visitor=ada, status=TicketStatus.OPEN)
    _ticket(db_session, tid, number="TKT-G-2", visitor=ada, status=TicketStatus.RESOLVED)

    assert _numbers(_search(pg_client, token, "lovelace", status_filter="resolved")) == {"TKT-G-2"}


def test_total_and_pagination_reflect_search(pg_client, db_session):
    user, token = _make_tenant_user(db_session)
    tid = user.tenant_id
    ada = _visitor(db_session, tid, name="Ada Lovelace", email="ada@example.test")
    # Matches on several fields at once must not duplicate rows or inflate total.
    for i in range(5):
        _ticket(
            db_session,
            tid,
            number=f"TKT-ada-{i}",
            summary="ada needs help",
            visitor=ada,
            user_identifier="ada-session",
        )
    _ticket(db_session, tid, number="TKT-ZZ-1", summary="unrelated")

    page1 = _search(pg_client, token, "ada", limit=2, offset=0).json()
    page3 = _search(pg_client, token, "ada", limit=2, offset=4).json()

    assert page1["total"] == 5
    assert len(page1["tickets"]) == 2
    assert page1["page"] == 1
    assert page3["total"] == 5
    assert len(page3["tickets"]) == 1
    assert page3["page"] == 3


def test_tenant_isolation(pg_client, db_session):
    me, token_me = _make_tenant_user(db_session)
    other, _token_other = _make_tenant_user(db_session)
    other_visitor = _visitor(db_session, other.tenant_id, name="Shared Name")
    _ticket(db_session, me.tenant_id, number="TKT-T-MINE", summary="shared keyword")
    _ticket(
        db_session,
        other.tenant_id,
        number="TKT-T-THEIRS",
        summary="shared keyword",
        visitor=other_visitor,
    )

    assert _numbers(_search(pg_client, token_me, "shared")) == {"TKT-T-MINE"}
    assert _search(pg_client, token_me, "shared").json()["total"] == 1


def test_ticket_with_visitorless_session_still_matches_own_fields(pg_client, db_session):
    user, token = _make_tenant_user(db_session)
    tid = user.tenant_id
    _ticket(db_session, tid, number="TKT-N-77", summary="no visitor here", visitor=None)
    _ticket(db_session, tid, number="TKT-N-88", summary="other")

    assert _numbers(_search(pg_client, token, "N-77")) == {"TKT-N-77"}
    assert _numbers(_search(pg_client, token, "no visitor")) == {"TKT-N-77"}


def test_visitor_with_null_fields_does_not_break_search(pg_client, db_session):
    user, token = _make_tenant_user(db_session)
    tid = user.tenant_id
    anon = _visitor(db_session, tid)  # name/email/phone all NULL
    _ticket(db_session, tid, number="TKT-X-1", summary="findme", visitor=anon)

    assert _numbers(_search(pg_client, token, "findme")) == {"TKT-X-1"}
    assert _numbers(_search(pg_client, token, "nomatch")) == set()
