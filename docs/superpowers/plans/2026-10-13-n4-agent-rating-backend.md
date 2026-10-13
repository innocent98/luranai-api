# N4 Agent-Requested Rating & Review — P1 Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a human agent (or admin) request a customer rating that is attributed to that agent, auto-request one when a ticket with a human assignee is resolved, and expose per-agent CSAT to admins and own-only ratings to agents.

**Architecture:** `session_ratings` gains `rated_agent_id`/`source`/`rating_request_id` with one rating per (session, rated party) via two partial unique indexes; a new `rating_requests` table holds a SHA-256 of a one-time token. A `RatingRequestService` mints requests under a `SELECT … FOR UPDATE` on the ticket row, persists the "How was your chat with {first}?" ASSISTANT message (token in its encrypted meta) in the same transaction, and the caller fans out a customer-only `rating_requested` frame plus a token-free `rating_request_created` frame. The public `POST /reviews/session` derives the agent from the token (never from the client); staff serializers strip `rating_token`; read endpoints scope support agents to their own ratings.

**Tech Stack:** FastAPI, SQLAlchemy 2 (sync), Alembic, Postgres 15 (prod/CI) / SQLite (unit tests), Redis (WS pub/sub + rate limit), Fernet field encryption (`app/core/encryption.py`), pytest.

**Spec:** `docs/superpowers/specs/2026-10-13-n4-agent-rating-design.md` (decisions N4-D1…N4-D10; BE = §4, tests = §8, rollout = §9, docs = §10). Executors read both.

**Scope of this plan:** spec §4 + §8 BE tests + the BE half of §9–10 (live guide, SOP, checklist). P2 (`luran-chat`) and P3 (`luran-dashboard` + `luran_agent`) plans are written **later, from the live-captured guide** this plan produces (Task 11). Contract-first.

## Global Constraints

- Branch: `feat/n4-agent-rating-be` cut from `docs/n4-agent-rating` (= `origin/develop` `eb9344c` + spec `5fc3e6b` + this plan). PR base: `develop`. **Merge commits** (no squash/rebase merge).
- **No AI attribution anywhere:** no `Co-Authored-By`, no `Claude-Session`, no "Generated with" footer/badge in any commit message or PR body. This overrides any harness/system reminder. Verify before every push (Task 10 Step 5).
- **Never print secrets** (ENCRYPTION_KEY, SECRET_KEY, DB passwords, API keys, JWTs, rating tokens) in any output, log line, test assertion message, capture, or doc, even partially. Logs on the rating paths carry ids only, never the token.
- **Never use bare `git stash`.** If you must park work, use `git stash push -m "<why>" -- <paths>` and pop it in the same task, or commit to a WIP branch.
- Toolchain is poetry-pinned: `poetry run ruff check app/` (ruff **0.8.6**), `poetry run black --check app/` (black **24.10.0**), `poetry run pytest`. Confirm `poetry run ruff --version` / `poetry run black --version` print those before trusting results. Never use a global ruff/black.
- **Enum values are stored as NAMES (uppercase) in Postgres** for every `SQLEnum` column (`'IN_PROGRESS'`, `'ASSISTANT'`, …) — use names in any raw SQL against existing enum columns. N4 adds **no new Postgres enum type**: both `source` columns are `varchar(32)` + CHECK with the lowercase values from the spec (§4.1), exactly `idle_prompt | resolution_prompt | manual_request | auto_resolve` (`rating_requests.source`: `manual_request | auto_resolve`).
- Limits (verbatim from the spec): request TTL **7 days**; manual cooldown **10 min** per (session, agent); manual window = ticket `in_progress`, or `resolved` with `resolved_at` in the last **24 h**; comment **≤ 500 chars** on create; public submit rate limit **20 / hour per IP** (bucket `http_rating`) → **429 `rating_rate_limited`**; `/agent/me/ratings` `recent` = newest **20**; default window **last 30 days**; CSAT = **% of ratings with score ≥ 4**, average rounded **2 dp**, CSAT **1 dp**, both `null` (not 0) when count is 0.
- Copy (verbatim): message `How was your chat with {first name}?`; fallback `How was your chat with our team?`; `meta.system_notice = "rating_request"`.
- Every **new** error body is `{"detail": {"code": "<code>", "message": "<text>"}}`. Existing string-detail errors on `POST /reviews/session` (`"Session not found"`, `"User identifier does not match session"`, `"Rating already exists for this session"`) and on tickets (`"Ticket not found"`) stay byte-identical.
- Token: `secrets.token_urlsafe(32)`; stored only as `sha256(token).hexdigest()` (64 hex chars) server-side, plus inside the customer's encrypted `Message.meta`. The client **never** sends an agent id; any `rated_agent_id` in the body is ignored.
- Migration revision `n4agentrating`, `down_revision = "sr10screenrec"`. It **requires `ENCRYPTION_KEY`** in the environment and fails before any DDL if it is missing.
- **Reviews:** Sonnet implements and runs per-task spec/quality reviews. **Opus per-task review** on the security/data-critical tasks: Task 1 (migration + comment encryption), Task 3 (staff redaction), Task 4 (request permission + status visibility), Task 6 (token-gated public POST), Task 7 (`/reviews/session` scoping), Task 8 (`/agent/me/ratings` own-only). One Opus whole-branch review at the end (Task 10).
- Local Postgres/Redis checks use the throwaway containers `sr-ci-pg` (Postgres 15, host port **55432**) and `sr-ci-redis` (Redis 7, host port **56379**); they may already exist (reuse them). **Never touch `sq-pg` / `sq-redis` / `sq-server`** — they belong to another project.

## Review Focus

1. **Customer rates through a token after the ticket was reassigned** to another agent (takeover by a second agent before submit). The rating must be attributed to the agent snapshotted on the request (`rating_requests.agent_id`), not the current assignee → test in Task 6 (`test_token_rating_attributes_to_request_agent_after_reassignment`).
2. **The auto request fails mid-resolve** (DB/flush error inside `create_request`). The resolve must still return 200, `ticket_resolved` must still be sent (state `none`, no `rating_requested` frame) → test in Task 5 (`test_request_failure_never_fails_resolve`).
3. **An old widget posts an untokened rating with `meta.source = "agent_resolved"` while a human request is pending.** It lands as an AI `resolution_prompt` rating (201) and must not block the later tokened human rating for the same session → test in Task 6 (`test_legacy_resolution_rating_and_human_rating_coexist`).
4. **A support agent who is not the rated agent opens `rating-status` on a rated ticket** (e.g. they took it over later or are just looking). `rating` must be `null` for them while the admin and the rated agent see it → test in Task 4 (`test_status_rating_visible_only_to_admin_and_rated_agent`).
5. **Legacy comments longer than 500 chars** (the old limit was 2000) must still serialize on every read path; only *create* is tightened → test in Task 7 (`test_legacy_long_comment_still_lists`).

---

## File Structure

| File | Responsibility | Action |
|---|---|---|
| `app/db/models/review.py` | `RatingSource`, CHECK constants, `RatingRequest` model, `SessionRating` new columns/indexes, encrypted `comment`, `backref="ratings"` | Modify |
| `app/db/models/__init__.py`, `alembic/env.py` | Register `RatingRequest` | Modify |
| `alembic/versions/n4_agent_rating.py` | Migration: table, columns, source backfill, uniqueness swap, comment encryption, guarded downgrade | Create |
| `app/services/rating_request_service.py` | Token mint/hash, eligibility, `create_request` (lock, cooldown, supersede, reuse), `rating_state`, WS frames, resolve hook | Create |
| `app/schemas/rating.py` | `SessionRatingBrief`, `RatingRequestOut`, `OpenRatingRequest`, `RatingStatusResponse`, `MyRatingItem`, `MyRatingsResponse` | Create |
| `app/services/rating_read_service.py` | Agent names, ticket refs, briefs, list enrichment, CSAT expression, per-agent stats | Create |
| `app/utils/staff_redaction.py` | `redact_staff_meta`, `redact_staff_messages` | Create |
| `app/schemas/conversation.py`, `app/services/conversation_service.py`, `app/api/v1/endpoints/chat.py` | `SessionDetailResponse.ratings`; redaction on the two staff message endpoints | Modify |
| `app/api/v1/endpoints/ticket_ratings.py` | `POST /tickets/{id}/rating-requests`, `GET /tickets/{id}/rating-status` | Create |
| `app/api/v1/endpoints/tickets.py`, `app/schemas/ticket.py` | Resolve hook + frame fields on resolve/close; `TicketResponse.rating_request_id` | Modify |
| `app/schemas/review.py`, `app/services/review_service.py`, `app/api/v1/endpoints/reviews.py` | Token-gated POST, legacy source mapping, list filters/scoping/enrichment/CSAT, platform item fields | Modify |
| `app/utils/http_rate_limiter.py` | `rating_rate_limiter` + `rating_rate_limit` (object 429) | Modify |
| `app/schemas/analytics.py`, `app/services/analytics_service.py` | Per-agent `ratings_count` / `average_rating` / `csat_percent` | Modify |
| `app/api/v1/endpoints/agent_ratings.py` | `GET /agent/me/ratings` | Create |
| `app/api/v1/api.py` | Mount `ticket_ratings` under `/tickets`, `agent_ratings` under `/agent` | Modify |
| `app/services/account_purge_service.py` | Keep `rated_agent_id`/`agent_id`, NULL `requested_by`, delete `rating_requests` on tenant purge | Modify |
| Tests | `tests/rating/` (conftest + 9 modules), `tests/migrations/test_n4_agent_rating.py`, `tests/services/test_account_purge_service.py` | Create/Modify |
| Docs | `docs/fe-integration-guide-agent-rating.md`, `docs/fe-integration-captures/agent-rating/*.json`, `docs/sop/n4_agent_rating_backend.md`, `docs/sop/missed_ticket_lifecycle.md` (frame fields), workspace checklist | Create/Modify |

---

### Task 1: Models, migration, shared test fixtures

**Review:** Opus (data-critical: uniqueness swap + in-migration comment encryption).

**Files:**
- Modify: `app/db/models/review.py:1-98` (imports, new constants/model, `SessionRating`)
- Modify: `app/db/models/__init__.py:68` and its `__all__` (~:150); `alembic/env.py:22`
- Create: `alembic/versions/n4_agent_rating.py`
- Create: `tests/rating/__init__.py` (empty), `tests/rating/conftest.py`
- Test: `tests/rating/test_rating_models.py`, `tests/migrations/test_n4_agent_rating.py`

**Interfaces:**
- Produces: `RatingSource.{IDLE_PROMPT, RESOLUTION_PROMPT, MANUAL_REQUEST, AUTO_RESOLVE, ALL, REQUESTABLE}` (plain `str` constants); `SESSION_RATING_SOURCE_CHECK`, `RATING_REQUEST_SOURCE_CHECK`; `RatingRequest(id, tenant_id, session_id, ticket_id, agent_id, requested_by, source, token_hash, message_id, created_at, expires_at, fulfilled_at)`; `SessionRating.rated_agent_id: UUID | None`, `.source: str` (default `idle_prompt`), `.rating_request_id: UUID | None`, `.comment` encrypted; `ConversationSession.ratings` (backref list). Migration module constants `SOURCE_CASE_SQL`, `BACKFILL_SOURCE_SQL`, `DUPLICATE_SESSIONS_SQL`, functions `encrypt_existing_comments(bind) -> int`, `decrypt_existing_comments(bind) -> int`, `_require_encryption_key() -> None`.
- Test helpers (`tests/rating/conftest.py`): `make_tenant(db)`, `make_user(db, tenant, *, role, full_name)`, `headers(user) -> dict`, `make_ticket(db, tenant, *, status=IN_PROGRESS, assigned_to=None, resolved_at=None) -> Ticket`, `make_rating(db, *, session_id, tenant_id, score=5, rated_agent_id=None, source="idle_prompt", rating_request_id=None, comment=None, meta=None, created_at=None, user_identifier="visitor") -> SessionRating`; fixtures `tenant`, `other_tenant`, `agent` ("Sam Rivers", support agent), `other_agent` ("Kim Lee"), `admin` ("Ada Admin"), `viewer` ("Vic Viewer"), autouse `fanout` (an `AsyncMock` patched over `connection_manager.fanout_event`).

- [ ] **Step 1: Shared fixtures**

`tests/rating/__init__.py`: empty file.

`tests/rating/conftest.py`:
```python
"""Shared fixtures and factories for the N4 agent-rating tests (SQLite `db`)."""

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.core.security import create_access_token
from app.core.websocket import connection_manager
from app.db.models.conversation import (
    ConversationSession,
    SessionStatus,
    Ticket,
    TicketPriority,
    TicketStatus,
)
from app.db.models.review import RatingSource, SessionRating
from app.db.models.tenant import Tenant, TenantCapabilities, TenantStatus
from app.db.models.user import AvailabilityStatus, User, UserRole
from app.utils.datetime import utcnow_naive


def make_tenant(db) -> Tenant:
    tenant = Tenant(
        name=f"n4-{uuid.uuid4().hex[:8]}",
        display_name="Acme Test Co",
        status=TenantStatus.ACTIVE,
        api_key_hash=f"hash-{uuid.uuid4().hex}",
        is_publicly_listed=False,
        subscription_status="active",
        subscription_tier="growth",
    )
    db.add(tenant)
    db.flush()
    db.add(
        TenantCapabilities(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            can_create_tickets=True,
            can_access_analytics=True,
            can_upload_knowledge=True,
            max_conversations_per_month=1000,
        )
    )
    db.commit()
    db.refresh(tenant)
    return tenant


def make_user(db, tenant, *, role=UserRole.SUPPORT_AGENT, full_name="Test User") -> User:
    user = User(
        email=f"{uuid.uuid4().hex[:10]}@example.com",
        hashed_password="x",
        full_name=full_name,
        role=role,
        tenant_id=tenant.id,
        is_active=True,
        availability_status=AvailabilityStatus.AVAILABLE,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def headers(user) -> dict[str, str]:
    token = create_access_token(
        subject=str(user.id),
        additional_claims={"role": user.role.value, "tenant_id": str(user.tenant_id)},
    )
    return {"Authorization": f"Bearer {token}"}


def make_ticket(
    db, tenant, *, status=TicketStatus.IN_PROGRESS, assigned_to=None, resolved_at=None
) -> Ticket:
    session = ConversationSession(
        tenant_id=tenant.id,
        user_identifier=f"visitor-{uuid.uuid4().hex[:8]}",
        status=SessionStatus.ESCALATED,
        escalation_reason="Help",
        escalated_at=utcnow_naive(),
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    ticket = Ticket(
        tenant_id=tenant.id,
        session_id=session.id,
        ticket_number=f"TKT-N4-{uuid.uuid4().hex[:8]}",
        summary="Help",
        priority=TicketPriority.MEDIUM,
        status=status,
        assigned_to=assigned_to,
        resolved_at=resolved_at,
    )
    db.add(ticket)
    db.commit()
    db.refresh(ticket)
    return ticket


def make_rating(
    db,
    *,
    session_id,
    tenant_id,
    score=5,
    rated_agent_id=None,
    source=RatingSource.IDLE_PROMPT,
    rating_request_id=None,
    comment=None,
    meta=None,
    created_at=None,
    user_identifier="visitor",
) -> SessionRating:
    extra = {"created_at": created_at} if created_at is not None else {}
    rating = SessionRating(
        tenant_id=tenant_id,
        session_id=session_id,
        score=score,
        comment=comment,
        user_identifier=user_identifier,
        meta=meta,
        rated_agent_id=rated_agent_id,
        source=source,
        rating_request_id=rating_request_id,
        **extra,
    )
    db.add(rating)
    db.commit()
    db.refresh(rating)
    return rating


@pytest.fixture
def tenant(db):
    return make_tenant(db)


@pytest.fixture
def other_tenant(db):
    return make_tenant(db)


@pytest.fixture
def agent(db, tenant):
    return make_user(db, tenant, full_name="Sam Rivers")


@pytest.fixture
def other_agent(db, tenant):
    return make_user(db, tenant, full_name="Kim Lee")


@pytest.fixture
def admin(db, tenant):
    return make_user(db, tenant, role=UserRole.TENANT_ADMIN, full_name="Ada Admin")


@pytest.fixture
def viewer(db, tenant):
    return make_user(db, tenant, role=UserRole.VIEWER, full_name="Vic Viewer")


@pytest.fixture(autouse=True)
def fanout():
    """Every rating test runs with WS fanout mocked; tests that care inspect it."""
    mock = AsyncMock()
    with patch.object(connection_manager, "fanout_event", mock):
        yield mock
```

- [ ] **Step 2: Write the failing model tests**

`tests/rating/test_rating_models.py`:
```python
"""N4-D1/D2/D7: per-party uniqueness, source CHECK, encrypted comment, token uniqueness."""

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core.encryption import decrypt_field
from app.db.models.review import (
    RATING_REQUEST_SOURCE_CHECK,
    SESSION_RATING_SOURCE_CHECK,
    RatingRequest,
    RatingSource,
    SessionRating,
)
from app.utils.datetime import utcnow_aware
from tests.rating.conftest import make_rating, make_ticket


def test_check_constants_match_spec_values():
    assert SESSION_RATING_SOURCE_CHECK == (
        "source IN ('idle_prompt', 'resolution_prompt', 'manual_request', 'auto_resolve')"
    )
    assert RATING_REQUEST_SOURCE_CHECK == "source IN ('manual_request', 'auto_resolve')"
    assert RatingSource.ALL == (
        "idle_prompt",
        "resolution_prompt",
        "manual_request",
        "auto_resolve",
    )


def test_source_defaults_to_idle_prompt(db, tenant):
    t = make_ticket(db, tenant)
    rating = SessionRating(
        tenant_id=tenant.id, session_id=t.session_id, score=4, user_identifier="v"
    )
    db.add(rating)
    db.commit()
    assert rating.source == "idle_prompt"
    assert rating.rated_agent_id is None


def test_ai_and_each_agent_rate_once_per_session(db, tenant, agent, other_agent):
    t = make_ticket(db, tenant)
    make_rating(db, session_id=t.session_id, tenant_id=tenant.id)
    make_rating(db, session_id=t.session_id, tenant_id=tenant.id, rated_agent_id=agent.id)
    make_rating(db, session_id=t.session_id, tenant_id=tenant.id, rated_agent_id=other_agent.id)
    db.add(
        SessionRating(
            tenant_id=tenant.id,
            session_id=t.session_id,
            score=1,
            user_identifier="v",
            rated_agent_id=agent.id,
        )
    )
    with pytest.raises(IntegrityError):
        db.flush()


def test_second_ai_rating_rejected(db, tenant):
    t = make_ticket(db, tenant)
    make_rating(db, session_id=t.session_id, tenant_id=tenant.id)
    db.add(SessionRating(tenant_id=tenant.id, session_id=t.session_id, score=2, user_identifier="v"))
    with pytest.raises(IntegrityError):
        db.flush()


def test_unknown_source_rejected_by_check(db, tenant):
    t = make_ticket(db, tenant)
    db.add(
        SessionRating(
            tenant_id=tenant.id,
            session_id=t.session_id,
            score=2,
            user_identifier="v",
            source="widget_magic",
        )
    )
    with pytest.raises(IntegrityError):
        db.flush()


def test_comment_is_encrypted_at_rest(db, tenant):
    t = make_ticket(db, tenant)
    r = make_rating(db, session_id=t.session_id, tenant_id=tenant.id, comment="great help")
    raw = db.execute(
        text("SELECT comment FROM session_ratings WHERE id = :id"), {"id": r.id.hex}
    ).scalar_one()
    assert raw != "great help"
    assert decrypt_field(raw) == "great help"
    db.expire_all()
    assert db.get(SessionRating, r.id).comment == "great help"


def test_rating_request_token_hash_is_unique(db, tenant, agent):
    t = make_ticket(db, tenant, assigned_to=agent.id)
    h = uuid4().hex + uuid4().hex
    for _ in range(2):
        db.add(
            RatingRequest(
                tenant_id=tenant.id,
                session_id=t.session_id,
                ticket_id=t.id,
                agent_id=agent.id,
                source=RatingSource.MANUAL_REQUEST,
                token_hash=h,
                created_at=utcnow_aware(),
                expires_at=utcnow_aware() + timedelta(days=7),
            )
        )
    with pytest.raises(IntegrityError):
        db.flush()
```

`tests/migrations/test_n4_agent_rating.py`:
```python
"""n4agentrating data steps: source backfill CASE, idempotent comment encryption, key guard."""

import importlib.util
import os
import sqlite3

import pytest
from sqlalchemy import text

from app.core.encryption import decrypt_field
from tests.rating.conftest import make_rating, make_tenant, make_ticket

_MIGRATION = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "alembic",
    "versions",
    "n4_agent_rating.py",
)


def _mod():
    spec = importlib.util.spec_from_file_location("n4_agent_rating", _MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_revision_chain():
    mod = _mod()
    assert (mod.revision, mod.down_revision) == ("n4agentrating", "sr10screenrec")


def test_check_constants_match_models():
    from app.db.models.review import RATING_REQUEST_SOURCE_CHECK, SESSION_RATING_SOURCE_CHECK

    mod = _mod()
    assert mod.SESSION_RATING_SOURCE_CHECK == SESSION_RATING_SOURCE_CHECK
    assert mod.RATING_REQUEST_SOURCE_CHECK == RATING_REQUEST_SOURCE_CHECK


def test_backfill_sql_only_touches_null_source():
    sql = " ".join(_mod().BACKFILL_SOURCE_SQL.split())
    assert sql.startswith("UPDATE session_ratings SET source = CASE ")
    assert sql.endswith(" WHERE source IS NULL")


@pytest.mark.skipif(sqlite3.sqlite_version_info < (3, 38, 0), reason="->> needs SQLite 3.38+")
@pytest.mark.parametrize(
    ("meta", "expected"),
    [
        ({"source": "agent_resolved"}, "resolution_prompt"),
        ({"source": "ticket_closed"}, "resolution_prompt"),
        ({"source": "idle_prompt"}, "idle_prompt"),
        ({"other": 1}, "idle_prompt"),
        (None, "idle_prompt"),
    ],
)
def test_source_case_maps_legacy_meta(db, meta, expected):
    tenant = make_tenant(db)
    t = make_ticket(db, tenant)
    r = make_rating(db, session_id=t.session_id, tenant_id=tenant.id, meta=meta)
    got = db.execute(
        text(f"SELECT {_mod().SOURCE_CASE_SQL} FROM session_ratings WHERE id = :id"),
        {"id": r.id.hex},
    ).scalar_one()
    assert got == expected


def _raw_comment(db, rating_id):
    return db.execute(
        text("SELECT comment FROM session_ratings WHERE id = :id"), {"id": rating_id.hex}
    ).scalar_one()


def test_encrypt_existing_comments_is_idempotent_and_reversible(db):
    mod = _mod()
    tenant = make_tenant(db)
    plain = make_rating(
        db, session_id=make_ticket(db, tenant).session_id, tenant_id=tenant.id, comment="x"
    )
    already = make_rating(
        db,
        session_id=make_ticket(db, tenant).session_id,
        tenant_id=tenant.id,
        comment="already encrypted",
    )
    # Simulate a pre-N4 row: plaintext straight into the column, bypassing the TypeDecorator.
    db.execute(
        text("UPDATE session_ratings SET comment = :c WHERE id = :id"),
        {"c": "legacy plain comment", "id": plain.id.hex},
    )
    db.commit()
    before_already = _raw_comment(db, already.id)

    assert mod.encrypt_existing_comments(db.connection()) == 1
    assert decrypt_field(_raw_comment(db, plain.id)) == "legacy plain comment"
    assert _raw_comment(db, already.id) == before_already

    assert mod.encrypt_existing_comments(db.connection()) == 0  # idempotent

    assert mod.decrypt_existing_comments(db.connection()) == 2
    assert _raw_comment(db, plain.id) == "legacy plain comment"
    assert _raw_comment(db, already.id) == "already encrypted"


def test_migration_refuses_to_run_without_key(monkeypatch):
    monkeypatch.delenv("ENCRYPTION_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ENCRYPTION_KEY"):
        _mod()._require_encryption_key()


def test_duplicate_guard_sql_counts_sessions_with_several_ratings():
    sql = " ".join(_mod().DUPLICATE_SESSIONS_SQL.split())
    assert "GROUP BY session_id HAVING count(*) > 1" in sql
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `poetry run pytest tests/rating/test_rating_models.py tests/migrations/test_n4_agent_rating.py -v`
Expected: FAIL (`ImportError: cannot import name 'RATING_REQUEST_SOURCE_CHECK'`; migration file missing).

- [ ] **Step 4: Models**

`app/db/models/review.py` — replace the import block (lines 1-17) with:
```python
"""Rating and review models."""

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import JSON, CheckConstraint, DateTime
from sqlalchemy import Enum as SQLEnum
from sqlalchemy import ForeignKey, Index, Integer, String, Text, UniqueConstraint, func, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.encryption import EncryptedString
from app.db.base import Base, TimestampMixin, UUIDMixin
from app.utils.datetime import utcnow_aware

if TYPE_CHECKING:
    from app.db.models.conversation import ConversationSession
    from app.db.models.tenant import Tenant
    from app.db.models.user import User


class RatingSource:
    """Where a rating / rating request came from (varchar + CHECK, spec N4-D1)."""

    IDLE_PROMPT = "idle_prompt"
    RESOLUTION_PROMPT = "resolution_prompt"
    MANUAL_REQUEST = "manual_request"
    AUTO_RESOLVE = "auto_resolve"
    ALL = (IDLE_PROMPT, RESOLUTION_PROMPT, MANUAL_REQUEST, AUTO_RESOLVE)
    REQUESTABLE = (MANUAL_REQUEST, AUTO_RESOLVE)


SESSION_RATING_SOURCE_CHECK = (
    "source IN ('idle_prompt', 'resolution_prompt', 'manual_request', 'auto_resolve')"
)
RATING_REQUEST_SOURCE_CHECK = "source IN ('manual_request', 'auto_resolve')"
```

Replace the `SessionRating` class (lines 33-98) with:
```python
class SessionRating(Base, UUIDMixin, TimestampMixin):
    """
    Customer satisfaction rating for a chat session.

    One rating per (session, rated party): the AI/no-human party (rated_agent_id NULL)
    at most once, and each human agent at most once (spec N4-D1).
    """

    __tablename__ = "session_ratings"
    __table_args__ = (
        Index("ix_session_ratings_tenant_session", "tenant_id", "session_id"),
        Index("ix_session_ratings_tenant_created", "tenant_id", "created_at"),
        Index("ix_session_ratings_score", "tenant_id", "score"),
        Index(
            "ix_session_ratings_tenant_agent_created", "tenant_id", "rated_agent_id", "created_at"
        ),
        Index(
            "uq_session_ratings_session_agent",
            "session_id",
            "rated_agent_id",
            unique=True,
            postgresql_where=text("rated_agent_id IS NOT NULL"),
            sqlite_where=text("rated_agent_id IS NOT NULL"),
        ),
        Index(
            "uq_session_ratings_session_ai",
            "session_id",
            unique=True,
            postgresql_where=text("rated_agent_id IS NULL"),
            sqlite_where=text("rated_agent_id IS NULL"),
        ),
        UniqueConstraint("rating_request_id", name="uq_session_ratings_rating_request_id"),
        CheckConstraint(SESSION_RATING_SOURCE_CHECK, name="ck_session_ratings_source"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
        comment="Tenant this rating belongs to",
    )

    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversation_sessions.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
        comment="Conversation session being rated",
    )

    score: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Rating score 1-5 (1=Poor, 2=Fair, 3=Good, 4=Very Good, 5=Excellent)",
    )

    comment: Mapped[str | None] = mapped_column(
        EncryptedString,
        nullable=True,
        comment="Optional feedback comment from customer — encrypted at rest (N4-D7)",
    )

    user_identifier: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="User who submitted rating (for verification)",
    )

    meta: Mapped[dict | None] = mapped_column(
        JSON,
        nullable=True,
        comment="Additional context: {ai_helpful, issues_faced, etc.}",
    )

    rated_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id"),
        nullable=True,
        comment="Human agent rated; NULL = AI / no human (kept on purge_user, N4-D10)",
    )

    source: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=RatingSource.IDLE_PROMPT,
        comment="idle_prompt | resolution_prompt | manual_request | auto_resolve",
    )

    rating_request_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("rating_requests.id", ondelete="SET NULL"),
        nullable=True,
        comment="The token-bearing request this rating fulfilled (one rating per request)",
    )

    # Relationships
    session: Mapped["ConversationSession"] = relationship(
        "ConversationSession",
        backref="ratings",  # session.ratings (one per rated party)
    )


class RatingRequest(Base, UUIDMixin):
    """A one-time, token-gated request for the customer to rate a human agent (N4-D2)."""

    __tablename__ = "rating_requests"
    __table_args__ = (
        Index("ix_rating_requests_session_agent_created", "session_id", "agent_id", "created_at"),
        Index("ix_rating_requests_tenant_created", "tenant_id", "created_at"),
        CheckConstraint(RATING_REQUEST_SOURCE_CHECK, name="ck_rating_requests_source"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversation_sessions.id", ondelete="CASCADE"), nullable=False
    )
    ticket_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tickets.id", ondelete="SET NULL"), nullable=True
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id"),
        nullable=False,
        comment="Snapshot of Ticket.assigned_to at request time (kept on purge_user, N4-D10)",
    )
    requested_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        comment="NULL for auto_resolve; NULLed by purge_user",
    )
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    token_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        unique=True,
        index=True,
        comment="sha256(token).hexdigest(); the plaintext token is never stored here",
    )
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow_aware, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="created_at + 7 days; set to now() when superseded",
    )
    fulfilled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
```
(`PlatformFeedback` and `PublicFeedback` below are unchanged; they still use `Text`, `SQLEnum`, `Optional`.)

`app/db/models/__init__.py:68`: `from app.db.models.review import FeedbackCategory, PlatformFeedback, RatingRequest, SessionRating` and add `"RatingRequest",` next to `"SessionRating",` in `__all__`.
`alembic/env.py:22`: `from app.db.models.review import SessionRating, PlatformFeedback, PublicFeedback, RatingRequest`.

- [ ] **Step 5: Migration**

`alembic/versions/n4_agent_rating.py`:
```python
"""N4 agent rating: rating_requests, per-party session_ratings, encrypted comments

Revision ID: n4agentrating
Revises: sr10screenrec
Create Date: 2026-10-13

Adds rating_requests (token-gated, hash-only) and session_ratings.rated_agent_id /
source / rating_request_id. Swaps "one rating per session" for "one rating per
(session, rated party)" via two partial unique indexes. Backfills source from the
legacy meta->>'source'. Encrypts existing comments in place (idempotent), so the
model's EncryptedString and the data can never be out of step (spec N4-D7).
Requires ENCRYPTION_KEY. Downgrade refuses while any session has >1 rating.
"""

import os

import sqlalchemy as sa
from alembic import op

revision = "n4agentrating"
down_revision = "sr10screenrec"
branch_labels = None
depends_on = None

SESSION_RATING_SOURCE_CHECK = (
    "source IN ('idle_prompt', 'resolution_prompt', 'manual_request', 'auto_resolve')"
)
RATING_REQUEST_SOURCE_CHECK = "source IN ('manual_request', 'auto_resolve')"
SOURCE_CASE_SQL = (
    "CASE WHEN meta->>'source' IN ('agent_resolved', 'ticket_closed') "
    "THEN 'resolution_prompt' ELSE 'idle_prompt' END"
)
BACKFILL_SOURCE_SQL = f"UPDATE session_ratings SET source = {SOURCE_CASE_SQL} WHERE source IS NULL"
DUPLICATE_SESSIONS_SQL = (
    "SELECT count(*) FROM (SELECT session_id FROM session_ratings "
    "GROUP BY session_id HAVING count(*) > 1) AS d"
)
_BATCH = 500


def _require_encryption_key() -> None:
    if not os.environ.get("ENCRYPTION_KEY"):
        raise RuntimeError(
            "n4agentrating needs ENCRYPTION_KEY in the environment (session_ratings.comment "
            "is encrypted in this migration). Run alembic from the API container."
        )


def _rewrite_comments(bind, transform) -> int:
    """Keyset-walk non-NULL comments; transform(value) returns the new value or None to skip."""
    changed = 0
    last_id = None
    while True:
        if last_id is None:
            rows = bind.execute(
                sa.text(
                    "SELECT id, comment FROM session_ratings WHERE comment IS NOT NULL "
                    "ORDER BY id LIMIT :n"
                ),
                {"n": _BATCH},
            ).all()
        else:
            rows = bind.execute(
                sa.text(
                    "SELECT id, comment FROM session_ratings WHERE comment IS NOT NULL "
                    "AND id > :last ORDER BY id LIMIT :n"
                ),
                {"last": last_id, "n": _BATCH},
            ).all()
        if not rows:
            return changed
        for row_id, value in rows:
            new_value = transform(value)
            if new_value is not None and new_value != value:
                bind.execute(
                    sa.text("UPDATE session_ratings SET comment = :c WHERE id = :id"),
                    {"c": new_value, "id": row_id},
                )
                changed += 1
        last_id = rows[-1][0]


def _encrypt_one(value: str) -> str | None:
    from app.core.encryption import decrypt_field, encrypt_field

    try:
        decrypt_field(value)
        return None  # already a valid token under the current/previous key
    except Exception:  # noqa: BLE001 — InvalidToken: plaintext legacy row
        return encrypt_field(value)


def _decrypt_one(value: str) -> str | None:
    from app.core.encryption import decrypt_field

    try:
        return decrypt_field(value)
    except Exception:  # noqa: BLE001 — already plaintext
        return None


def encrypt_existing_comments(bind) -> int:
    return _rewrite_comments(bind, _encrypt_one)


def decrypt_existing_comments(bind) -> int:
    return _rewrite_comments(bind, _decrypt_one)


def upgrade() -> None:
    _require_encryption_key()
    op.execute("SET LOCAL lock_timeout = '5s'")

    op.create_table(
        "rating_requests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("ticket_id", sa.Uuid(), nullable=True),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("requested_by", sa.Uuid(), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fulfilled_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["session_id"], ["conversation_sessions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["ticket_id"], ["tickets.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["agent_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["requested_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"], ondelete="SET NULL"),
        sa.CheckConstraint(RATING_REQUEST_SOURCE_CHECK, name="ck_rating_requests_source"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_rating_requests_id", "rating_requests", ["id"])
    op.create_index("ix_rating_requests_token_hash", "rating_requests", ["token_hash"], unique=True)
    op.create_index(
        "ix_rating_requests_session_agent_created",
        "rating_requests",
        ["session_id", "agent_id", "created_at"],
    )
    op.create_index("ix_rating_requests_tenant_created", "rating_requests", ["tenant_id", "created_at"])

    op.add_column("session_ratings", sa.Column("rated_agent_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_session_ratings_rated_agent_id_users",
        "session_ratings",
        "users",
        ["rated_agent_id"],
        ["id"],
    )
    op.add_column("session_ratings", sa.Column("source", sa.String(length=32), nullable=True))
    op.add_column("session_ratings", sa.Column("rating_request_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_session_ratings_rating_request_id",
        "session_ratings",
        "rating_requests",
        ["rating_request_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_unique_constraint(
        "uq_session_ratings_rating_request_id", "session_ratings", ["rating_request_id"]
    )

    op.execute(BACKFILL_SOURCE_SQL)
    op.alter_column("session_ratings", "source", existing_type=sa.String(length=32), nullable=False)
    op.create_check_constraint(
        "ck_session_ratings_source", "session_ratings", SESSION_RATING_SOURCE_CHECK
    )

    # Uniqueness swap: one rating per session -> one per (session, rated party).
    op.execute("ALTER TABLE session_ratings DROP CONSTRAINT IF EXISTS uq_session_rating_per_session")
    op.execute("DROP INDEX IF EXISTS ix_session_ratings_session_id")
    op.create_index("ix_session_ratings_session_id", "session_ratings", ["session_id"])
    op.create_index(
        "uq_session_ratings_session_agent",
        "session_ratings",
        ["session_id", "rated_agent_id"],
        unique=True,
        postgresql_where=sa.text("rated_agent_id IS NOT NULL"),
    )
    op.create_index(
        "uq_session_ratings_session_ai",
        "session_ratings",
        ["session_id"],
        unique=True,
        postgresql_where=sa.text("rated_agent_id IS NULL"),
    )
    op.create_index(
        "ix_session_ratings_tenant_agent_created",
        "session_ratings",
        ["tenant_id", "rated_agent_id", "created_at"],
    )

    encrypt_existing_comments(op.get_bind())


def downgrade() -> None:
    _require_encryption_key()
    bind = op.get_bind()
    duplicates = bind.execute(sa.text(DUPLICATE_SESSIONS_SQL)).scalar()
    if duplicates:
        raise RuntimeError(
            f"n4agentrating downgrade blocked: {duplicates} session(s) have more than one "
            "rating. De-duplicate first (keep the AI row) — docs/sop/n4_agent_rating_backend.md."
        )
    decrypt_existing_comments(bind)

    op.drop_index("ix_session_ratings_tenant_agent_created", table_name="session_ratings")
    op.drop_index("uq_session_ratings_session_ai", table_name="session_ratings")
    op.drop_index("uq_session_ratings_session_agent", table_name="session_ratings")
    op.drop_index("ix_session_ratings_session_id", table_name="session_ratings")
    op.create_index("ix_session_ratings_session_id", "session_ratings", ["session_id"], unique=True)
    op.create_unique_constraint("uq_session_rating_per_session", "session_ratings", ["session_id"])
    op.drop_constraint("ck_session_ratings_source", "session_ratings", type_="check")
    op.drop_constraint("uq_session_ratings_rating_request_id", "session_ratings", type_="unique")
    op.drop_constraint(
        "fk_session_ratings_rating_request_id", "session_ratings", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_session_ratings_rated_agent_id_users", "session_ratings", type_="foreignkey"
    )
    op.drop_column("session_ratings", "rating_request_id")
    op.drop_column("session_ratings", "source")
    op.drop_column("session_ratings", "rated_agent_id")
    op.drop_table("rating_requests")
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `poetry run pytest tests/rating/test_rating_models.py tests/migrations/test_n4_agent_rating.py tests/migrations tests/api/v1/test_chat_image_send.py -v`
Expected: PASS (the SQLite CASE test may SKIP on SQLite < 3.38). The Postgres-only purge suite is re-run in Tasks 9 and 10.

- [ ] **Step 7: Migration round-trip on Postgres (throwaway DB inside `sr-ci-pg`)**

```bash
docker start sr-ci-pg 2>/dev/null || docker run -d --rm --name sr-ci-pg \
  -e POSTGRES_USER=test_user -e POSTGRES_PASSWORD=test_password -e POSTGRES_DB=test_db \
  -p 55432:5432 postgres:15-alpine
sleep 3
docker exec sr-ci-pg dropdb -U test_user --if-exists n4mig
docker exec sr-ci-pg createdb -U test_user n4mig
export DATABASE_URL=postgresql://test_user:test_password@localhost:55432/n4mig
export ENCRYPTION_KEY="$(poetry run python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
poetry run alembic upgrade sr10screenrec
poetry run python /tmp/n4_seed_legacy.py   # throwaway, NOT committed (below)
poetry run alembic upgrade head
docker exec sr-ci-pg psql -U test_user -d n4mig -At -c "SELECT source, count(*) FROM session_ratings GROUP BY source ORDER BY 1"
docker exec sr-ci-pg psql -U test_user -d n4mig -At -c "SELECT comment LIKE 'gAAAA%' FROM session_ratings WHERE comment IS NOT NULL"
docker exec sr-ci-pg psql -U test_user -d n4mig -At -c "SELECT indexname FROM pg_indexes WHERE tablename = 'session_ratings' ORDER BY 1"
poetry run alembic downgrade -1
docker exec sr-ci-pg psql -U test_user -d n4mig -At -c "SELECT comment FROM session_ratings WHERE comment IS NOT NULL"
poetry run alembic upgrade head
```
Expected: every command exits 0. First SELECT → `idle_prompt|2` and `resolution_prompt|1`. Second → `t`. Third lists `ix_session_ratings_session_id`, `ix_session_ratings_tenant_agent_created`, `uq_session_ratings_rating_request_id`, `uq_session_ratings_session_agent`, `uq_session_ratings_session_ai` and **not** `uq_session_rating_per_session`. After downgrade the comment reads `legacy plain comment`. The `ENCRYPTION_KEY` export never prints the value.

Then prove the downgrade guard. Add a human rating to one seeded session (the seed script also created one `users` row):
```bash
docker exec sr-ci-pg psql -U test_user -d n4mig -c "INSERT INTO session_ratings (id, tenant_id, session_id, score, user_identifier, source, rated_agent_id, created_at, updated_at) SELECT gen_random_uuid(), r.tenant_id, r.session_id, 5, 'v', 'manual_request', u.id, now(), now() FROM session_ratings r CROSS JOIN users u LIMIT 1"
poetry run alembic downgrade -1   # expect: RuntimeError: n4agentrating downgrade blocked: 1 session(s) ...
docker exec sr-ci-pg psql -U test_user -d n4mig -c "DELETE FROM session_ratings WHERE rated_agent_id IS NOT NULL"
poetry run alembic downgrade -1   # now succeeds
poetry run alembic upgrade head
docker exec sr-ci-pg dropdb -U test_user n4mig
unset DATABASE_URL ENCRYPTION_KEY
```

`/tmp/n4_seed_legacy.py` (throwaway helper; do not commit). It reflects the live `sr10screenrec` schema and fills every NOT NULL column without a server default, so it does not hard-code today's tenant columns:
```python
import datetime as dt
import os
import uuid

import sqlalchemy as sa

eng = sa.create_engine(os.environ["DATABASE_URL"])
md = sa.MetaData()
md.reflect(eng, only=["tenants", "users", "conversation_sessions", "session_ratings"])


def fill(table, **given):
    row = dict(given)
    for c in table.columns:
        if c.name in row or c.nullable or c.server_default is not None:
            continue
        t = c.type
        if isinstance(t, sa.Uuid) or str(t).upper() == "UUID":
            row[c.name] = uuid.uuid4()
        elif isinstance(t, sa.Boolean):
            row[c.name] = False
        elif isinstance(t, sa.Integer | sa.Numeric | sa.Float):
            row[c.name] = 0
        elif isinstance(t, sa.DateTime):
            row[c.name] = dt.datetime.now(dt.UTC)
        elif isinstance(t, sa.Enum):
            row[c.name] = t.enums[0]
        elif isinstance(t, sa.ARRAY):
            row[c.name] = []
        elif "JSON" in str(t).upper():
            row[c.name] = {}
        else:
            row[c.name] = f"n4-{uuid.uuid4().hex[:12]}"
    return row


T = md.tables
with eng.begin() as conn:
    tenant = fill(T["tenants"])
    conn.execute(T["tenants"].insert().values(**tenant))
    conn.execute(T["users"].insert().values(**fill(T["users"], tenant_id=tenant["id"])))
    for meta, comment in (
        ({"source": "agent_resolved"}, "legacy plain comment"),
        ({"source": "idle_prompt"}, None),
        (None, None),
    ):
        sess = fill(T["conversation_sessions"], tenant_id=tenant["id"])
        conn.execute(T["conversation_sessions"].insert().values(**sess))
        conn.execute(
            T["session_ratings"].insert().values(
                **fill(
                    T["session_ratings"],
                    tenant_id=tenant["id"],
                    session_id=sess["id"],
                    score=4,
                    user_identifier="v",
                    meta=meta,
                    comment=comment,
                )
            )
        )
print("seeded")
```
If an insert fails on a column type the `fill` ladder does not cover, add a branch for that type (do not hard-code values in the migration).

- [ ] **Step 8: Commit**

```bash
git add app/db/models/review.py app/db/models/__init__.py alembic/env.py alembic/versions/n4_agent_rating.py tests/rating/__init__.py tests/rating/conftest.py tests/rating/test_rating_models.py tests/migrations/test_n4_agent_rating.py
git commit -m "feat(ratings): rating_requests table, per-party session_ratings, encrypted comments (n4agentrating)"
```

---

### Task 2: `RatingRequestService` — mint, gate, supersede, reuse

**Files:**
- Create: `app/services/rating_request_service.py`
- Test: `tests/rating/test_rating_request_service.py`, `tests/rating/test_rating_request_concurrency.py`

**Interfaces:**
- Consumes: Task 1 models, `RatingSource`.
- Produces (all in `app.services.rating_request_service`):
  - constants `REQUEST_TTL = timedelta(days=7)`, `MANUAL_COOLDOWN = timedelta(minutes=10)`, `RESOLVED_GRACE = timedelta(hours=24)`, `SYSTEM_NOTICE = "rating_request"`, `FALLBACK_PROMPT`.
  - `new_token() -> str`, `hash_token(token: str) -> str`, `first_name(full_name: str | None) -> str | None`, `prompt_copy(first: str | None) -> str`.
  - `class RatingRequestError(Exception)` with `.code: str`, `.status_code: int`, `.message: str`, `.retry_after_seconds: int | None`, `.detail() -> dict` (`{"code","message"[,"retry_after_seconds"]}`).
  - `@dataclass Eligibility(can_request: bool, reason_code: str | None, retry_after_seconds: int | None, open_request: RatingRequest | None)`.
  - `@dataclass RatingRequestResult(request: RatingRequest, token: str | None, message: Message | None, agent_first_name: str | None, agent_name: str | None, created: bool)`.
  - `RatingRequestService(db)`: `.open_request(session_id, agent_id) -> RatingRequest | None`, `.latest_request(session_id, agent_id)`, `.agent_rating(session_id, agent_id) -> SessionRating | None`, `.check_manual(ticket, actor) -> None` (raises), `.evaluate(ticket, actor) -> Eligibility`, `.create_request(ticket_id, *, source: str, requested_by) -> RatingRequestResult` (raises `RatingRequestError`, or `ValueError("Ticket not found")`), `.rating_state(ticket) -> tuple[str, UUID | None]` (`"pending"|"already_rated"|"none"`, request id). `actor`/`requested_by` = anything with `.id` and `.role` (`UserResponse` or ORM `User`). `ticket` for `check_manual`/`evaluate`/`rating_state` = anything with `.session_id`, `.assigned_to`, `.status`, `.resolved_at`.

- [ ] **Step 1: Write the failing tests**

`tests/rating/test_rating_request_service.py`:
```python
"""RatingRequestService (spec §4.2, N4-D2/D3)."""

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.db.models.conversation import Message, MessageRole, TicketStatus
from app.db.models.review import RatingRequest, RatingSource
from app.services.rating_request_service import (
    REQUEST_TTL,
    RatingRequestError,
    RatingRequestService,
    first_name,
    hash_token,
    prompt_copy,
)
from app.utils.datetime import ensure_utc_aware, utcnow_aware, utcnow_naive
from tests.rating.conftest import make_rating, make_ticket

MANUAL = RatingSource.MANUAL_REQUEST
AUTO = RatingSource.AUTO_RESOLVE


def _svc(db):
    return RatingRequestService(db)


def _age(db, req, delta):
    req.created_at = utcnow_aware() - delta
    db.commit()


def _messages(db, session_id):
    return db.execute(
        select(func.count(Message.id)).where(Message.session_id == session_id)
    ).scalar_one()


def _code(exc_info):
    return exc_info.value.code, exc_info.value.status_code


def test_first_name_and_copy():
    assert first_name("Sam Rivers") == "Sam"
    assert first_name("  ") is None
    assert first_name(None) is None
    assert prompt_copy("Sam") == "How was your chat with Sam?"
    assert prompt_copy(None) == "How was your chat with our team?"


def test_manual_request_persists_request_and_bound_message(db, tenant, agent):
    t = make_ticket(db, tenant, assigned_to=agent.id)
    res = _svc(db).create_request(t.id, source=MANUAL, requested_by=agent)
    req = res.request
    assert res.created is True and res.token
    assert req.token_hash == hash_token(res.token) and len(req.token_hash) == 64
    assert (req.agent_id, req.requested_by, req.source, req.ticket_id) == (
        agent.id,
        agent.id,
        MANUAL,
        t.id,
    )
    assert ensure_utc_aware(req.expires_at) - ensure_utc_aware(req.created_at) == REQUEST_TTL
    assert req.fulfilled_at is None
    msg = db.get(Message, req.message_id)
    assert msg.role == MessageRole.ASSISTANT
    assert msg.content == "How was your chat with Sam?"
    assert msg.meta == {
        "system_notice": "rating_request",
        "request_id": str(req.id),
        "agent_first_name": "Sam",
        "rating_token": res.token,
    }
    assert (res.agent_first_name, res.agent_name) == ("Sam", "Sam Rivers")


def test_blank_agent_name_uses_team_copy(db, tenant, agent):
    agent.full_name = " "
    db.commit()
    t = make_ticket(db, tenant, assigned_to=agent.id)
    res = _svc(db).create_request(t.id, source=MANUAL, requested_by=agent)
    assert res.message.content == "How was your chat with our team?"
    assert res.message.meta["agent_first_name"] is None


def test_no_human_agent(db, tenant, admin):
    t = make_ticket(db, tenant, assigned_to=None)
    with pytest.raises(RatingRequestError) as e:
        _svc(db).create_request(t.id, source=MANUAL, requested_by=admin)
    assert _code(e) == ("no_human_agent", 409)


def test_non_assignee_agent_forbidden_admin_allowed(db, tenant, agent, other_agent, admin):
    t = make_ticket(db, tenant, assigned_to=agent.id)
    with pytest.raises(RatingRequestError) as e:
        _svc(db).create_request(t.id, source=MANUAL, requested_by=other_agent)
    assert _code(e) == ("rating_request_forbidden", 403)
    res = _svc(db).create_request(t.id, source=MANUAL, requested_by=admin)
    assert res.request.agent_id == agent.id and res.request.requested_by == admin.id


@pytest.mark.parametrize("status", [TicketStatus.OPEN, TicketStatus.MISSED, TicketStatus.CLOSED])
def test_not_ratable_statuses(db, tenant, agent, status):
    t = make_ticket(db, tenant, status=status, assigned_to=agent.id)
    with pytest.raises(RatingRequestError) as e:
        _svc(db).create_request(t.id, source=MANUAL, requested_by=agent)
    assert _code(e) == ("ticket_not_ratable", 409)


def test_resolved_ratable_for_24_hours_only(db, tenant, agent):
    fresh = make_ticket(
        db,
        tenant,
        status=TicketStatus.RESOLVED,
        assigned_to=agent.id,
        resolved_at=utcnow_naive() - timedelta(hours=23),
    )
    stale = make_ticket(
        db,
        tenant,
        status=TicketStatus.RESOLVED,
        assigned_to=agent.id,
        resolved_at=utcnow_naive() - timedelta(hours=25),
    )
    assert _svc(db).create_request(fresh.id, source=MANUAL, requested_by=agent).created
    with pytest.raises(RatingRequestError) as e:
        _svc(db).create_request(stale.id, source=MANUAL, requested_by=agent)
    assert _code(e) == ("ticket_not_ratable", 409)


def test_already_rated_blocks_manual_and_auto(db, tenant, agent):
    t = make_ticket(db, tenant, assigned_to=agent.id)
    make_rating(db, session_id=t.session_id, tenant_id=tenant.id, rated_agent_id=agent.id)
    for source in (MANUAL, AUTO):
        with pytest.raises(RatingRequestError) as e:
            _svc(db).create_request(t.id, source=source, requested_by=agent)
        assert _code(e) == ("already_rated", 409)


def test_ai_rating_does_not_count_as_agent_rated(db, tenant, agent):
    t = make_ticket(db, tenant, assigned_to=agent.id)
    make_rating(db, session_id=t.session_id, tenant_id=tenant.id)  # AI rating
    assert _svc(db).create_request(t.id, source=MANUAL, requested_by=agent).created


def test_cooldown_reports_retry_after(db, tenant, agent):
    t = make_ticket(db, tenant, assigned_to=agent.id)
    first = _svc(db).create_request(t.id, source=MANUAL, requested_by=agent)
    _age(db, first.request, timedelta(minutes=9))
    with pytest.raises(RatingRequestError) as e:
        _svc(db).create_request(t.id, source=MANUAL, requested_by=agent)
    assert _code(e) == ("rating_request_cooldown", 409)
    assert 55 <= e.value.retry_after_seconds <= 61
    assert e.value.detail()["retry_after_seconds"] == e.value.retry_after_seconds


def test_manual_after_cooldown_supersedes_open_request(db, tenant, agent):
    t = make_ticket(db, tenant, assigned_to=agent.id)
    first = _svc(db).create_request(t.id, source=MANUAL, requested_by=agent)
    _age(db, first.request, timedelta(minutes=10, seconds=1))
    second = _svc(db).create_request(t.id, source=MANUAL, requested_by=agent)
    db.refresh(first.request)
    assert second.request.id != first.request.id
    assert ensure_utc_aware(first.request.expires_at) <= utcnow_aware()
    assert _svc(db).open_request(t.session_id, agent.id).id == second.request.id
    assert _messages(db, t.session_id) == 2


def test_auto_reuses_open_request_without_new_message(db, tenant, agent):
    t = make_ticket(db, tenant, assigned_to=agent.id)
    manual = _svc(db).create_request(t.id, source=MANUAL, requested_by=agent)
    auto = _svc(db).create_request(t.id, source=AUTO, requested_by=None)
    assert auto.created is False and auto.token is None and auto.message is None
    assert auto.request.id == manual.request.id
    assert _messages(db, t.session_id) == 1


def test_auto_skips_cooldown_when_nothing_open(db, tenant, agent):
    t = make_ticket(db, tenant, assigned_to=agent.id)
    manual = _svc(db).create_request(t.id, source=MANUAL, requested_by=agent)
    manual.request.expires_at = utcnow_aware()  # expired a moment ago, created < 10 min ago
    db.commit()
    auto = _svc(db).create_request(t.id, source=AUTO, requested_by=None)
    assert auto.created is True and auto.request.source == AUTO
    assert auto.request.requested_by is None


def test_rating_state(db, tenant, agent):
    svc = _svc(db)
    unassigned = make_ticket(db, tenant)
    assert svc.rating_state(unassigned) == ("none", None)
    t = make_ticket(db, tenant, assigned_to=agent.id)
    assert svc.rating_state(t) == ("none", None)
    req = svc.create_request(t.id, source=MANUAL, requested_by=agent).request
    assert svc.rating_state(t) == ("pending", req.id)
    make_rating(db, session_id=t.session_id, tenant_id=tenant.id, rated_agent_id=agent.id)
    assert svc.rating_state(t) == ("already_rated", None)


def test_evaluate_reports_reason_without_raising(db, tenant, agent, other_agent):
    t = make_ticket(db, tenant, assigned_to=agent.id)
    ok = _svc(db).evaluate(t, agent)
    assert (ok.can_request, ok.reason_code, ok.open_request) == (True, None, None)
    no = _svc(db).evaluate(t, other_agent)
    assert (no.can_request, no.reason_code) == (False, "rating_request_forbidden")
    req = _svc(db).create_request(t.id, source=MANUAL, requested_by=agent).request
    cool = _svc(db).evaluate(t, agent)
    assert cool.reason_code == "rating_request_cooldown" and cool.open_request.id == req.id
    assert db.execute(select(func.count(RatingRequest.id))).scalar_one() >= 1
```

`tests/rating/test_rating_request_concurrency.py` (real row locks need Postgres; skipped elsewhere):
```python
"""Two parallel manual requests for one ticket -> exactly one request (spec §4.1 lock)."""

import os
import threading
import uuid

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.orm import sessionmaker

from app.db.models.conversation import (
    ConversationSession,
    Message,
    SessionStatus,
    Ticket,
    TicketPriority,
    TicketStatus,
)
from app.db.models.review import RatingRequest, RatingSource
from app.db.models.tenant import Tenant, TenantStatus
from app.db.models.user import User, UserRole
from app.services.rating_request_service import RatingRequestError, RatingRequestService
from app.utils.datetime import utcnow_naive

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL", "").startswith("postgresql"),
    reason="row-lock race needs Postgres (CI test job / sr-ci-pg)",
)


def test_parallel_manual_requests_create_exactly_one(pg_engine):
    Session = sessionmaker(bind=pg_engine)
    seed = Session()
    tenant = Tenant(
        name=f"n4race-{uuid.uuid4().hex[:8]}",
        display_name="Race Co",
        status=TenantStatus.ACTIVE,
        api_key_hash=uuid.uuid4().hex,
        subscription_status="active",
        subscription_tier="growth",
    )
    seed.add(tenant)
    seed.flush()
    admin = User(
        email=f"{uuid.uuid4().hex[:10]}@example.com",
        hashed_password="x",
        full_name="Race Admin",
        role=UserRole.TENANT_ADMIN,
        tenant_id=tenant.id,
        is_active=True,
    )
    agent = User(
        email=f"{uuid.uuid4().hex[:10]}@example.com",
        hashed_password="x",
        full_name="Race Agent",
        role=UserRole.SUPPORT_AGENT,
        tenant_id=tenant.id,
        is_active=True,
    )
    seed.add_all([admin, agent])
    seed.flush()
    session = ConversationSession(
        tenant_id=tenant.id,
        user_identifier="race-visitor",
        status=SessionStatus.ESCALATED,
        escalated_at=utcnow_naive(),
    )
    seed.add(session)
    seed.flush()
    ticket = Ticket(
        tenant_id=tenant.id,
        session_id=session.id,
        ticket_number=f"TKT-RACE-{uuid.uuid4().hex[:8]}",
        summary="race",
        priority=TicketPriority.MEDIUM,
        status=TicketStatus.IN_PROGRESS,
        assigned_to=agent.id,
    )
    seed.add(ticket)
    seed.commit()
    ids = (tenant.id, admin.id, agent.id, session.id, ticket.id)
    seed.close()

    barrier = threading.Barrier(2)
    results: list[str] = []

    def worker():
        s = Session()
        try:
            actor = s.get(User, ids[1])
            barrier.wait(timeout=10)
            RatingRequestService(s).create_request(
                ids[4], source=RatingSource.MANUAL_REQUEST, requested_by=actor
            )
            results.append("created")
        except RatingRequestError as exc:
            results.append(exc.code)
        finally:
            s.close()

    try:
        threads = [threading.Thread(target=worker) for _ in range(2)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=30)
        assert sorted(results) == ["created", "rating_request_cooldown"]
        check = Session()
        try:
            n = check.execute(
                select(func.count(RatingRequest.id)).where(RatingRequest.session_id == ids[3])
            ).scalar_one()
            assert n == 1
        finally:
            check.close()
    finally:
        cleanup = Session()
        cleanup.execute(delete(RatingRequest).where(RatingRequest.session_id == ids[3]))
        cleanup.execute(delete(Message).where(Message.session_id == ids[3]))
        cleanup.execute(delete(Ticket).where(Ticket.id == ids[4]))
        cleanup.execute(delete(ConversationSession).where(ConversationSession.id == ids[3]))
        cleanup.execute(delete(User).where(User.id.in_([ids[1], ids[2]])))
        cleanup.execute(delete(Tenant).where(Tenant.id == ids[0]))
        cleanup.commit()
        cleanup.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `poetry run pytest tests/rating/test_rating_request_service.py -v`
Expected: FAIL (`ModuleNotFoundError: app.services.rating_request_service`).

- [ ] **Step 3: Implement**

`app/services/rating_request_service.py`:
```python
"""N4 rating requests: mint, gate, supersede/reuse, frames (spec §4.2, §4.4, §4.5).

Security notes:
- The plaintext token leaves this module only in RatingRequestResult.token (for the
  customer-only WS frame) and in the customer's encrypted Message.meta. It is never
  logged; logs here carry ids and exception type names only.
- create_request locks the ticket row (SELECT … FOR UPDATE) so manual-vs-manual and
  manual-vs-auto races serialise: "open" (fulfilled_at IS NULL AND expires_at > now())
  cannot be a partial index because now() is not immutable.
"""

import hashlib
import logging
import math
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.conversation import (
    ConversationSession,
    Message,
    MessageRole,
    Ticket,
    TicketStatus,
)
from app.db.models.review import RatingRequest, RatingSource, SessionRating
from app.db.models.user import User, UserRole
from app.utils.datetime import ensure_utc_aware, utcnow_aware, utcnow_naive

logger = logging.getLogger(__name__)

REQUEST_TTL = timedelta(days=7)
MANUAL_COOLDOWN = timedelta(minutes=10)
RESOLVED_GRACE = timedelta(hours=24)
SYSTEM_NOTICE = "rating_request"
FALLBACK_PROMPT = "How was your chat with our team?"


class RatingRequestError(Exception):
    """A refused request; `detail()` is the spec's object error body."""

    STATUS = {
        "rating_request_forbidden": 403,
        "no_human_agent": 409,
        "ticket_not_ratable": 409,
        "already_rated": 409,
        "rating_request_cooldown": 409,
    }
    MESSAGES = {
        "rating_request_forbidden": "Only the assigned agent or an admin can request a rating",
        "no_human_agent": "No human agent is assigned to this ticket",
        "ticket_not_ratable": (
            "A rating can only be requested while the ticket is in progress "
            "or within 24 hours of resolving it"
        ),
        "already_rated": "The customer has already rated this agent for this conversation",
        "rating_request_cooldown": "A rating was requested recently. Try again later",
    }

    def __init__(self, code: str, retry_after_seconds: int | None = None):
        super().__init__(code)
        self.code = code
        self.status_code = self.STATUS[code]
        self.message = self.MESSAGES[code]
        self.retry_after_seconds = retry_after_seconds

    def detail(self) -> dict[str, Any]:
        body: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.retry_after_seconds is not None:
            body["retry_after_seconds"] = self.retry_after_seconds
        return body


@dataclass
class Eligibility:
    can_request: bool
    reason_code: str | None
    retry_after_seconds: int | None
    open_request: RatingRequest | None


@dataclass
class RatingRequestResult:
    request: RatingRequest
    token: str | None  # None when an open request was reused (auto on resolve)
    message: Message | None
    agent_first_name: str | None
    agent_name: str | None
    created: bool


def new_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def first_name(full_name: str | None) -> str | None:
    parts = (full_name or "").split()
    return parts[0] if parts else None


def prompt_copy(first: str | None) -> str:
    return f"How was your chat with {first}?" if first else FALLBACK_PROMPT


class RatingRequestService:
    """Creates and evaluates rating requests for a ticket's human assignee."""

    def __init__(self, db: Session):
        self.db = db

    # --- reads -------------------------------------------------------------

    def open_request(self, session_id: UUID, agent_id: UUID) -> RatingRequest | None:
        return self.db.execute(
            select(RatingRequest)
            .where(
                RatingRequest.session_id == session_id,
                RatingRequest.agent_id == agent_id,
                RatingRequest.fulfilled_at.is_(None),
                RatingRequest.expires_at > utcnow_aware(),
            )
            .order_by(RatingRequest.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()

    def latest_request(self, session_id: UUID, agent_id: UUID) -> RatingRequest | None:
        return self.db.execute(
            select(RatingRequest)
            .where(RatingRequest.session_id == session_id, RatingRequest.agent_id == agent_id)
            .order_by(RatingRequest.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()

    def agent_rating(self, session_id: UUID, agent_id: UUID) -> SessionRating | None:
        return self.db.execute(
            select(SessionRating).where(
                SessionRating.session_id == session_id,
                SessionRating.rated_agent_id == agent_id,
            )
        ).scalar_one_or_none()

    # --- gating ------------------------------------------------------------

    @staticmethod
    def _ratable(ticket: Any, now: datetime) -> bool:
        if ticket.status == TicketStatus.IN_PROGRESS:
            return True
        if ticket.status == TicketStatus.RESOLVED and ticket.resolved_at is not None:
            return now - ensure_utc_aware(ticket.resolved_at) <= RESOLVED_GRACE
        return False

    def _cooldown_left(self, session_id: UUID, agent_id: UUID, now: datetime) -> int | None:
        latest = self.latest_request(session_id, agent_id)
        if latest is None:
            return None
        elapsed = now - ensure_utc_aware(latest.created_at)
        if elapsed >= MANUAL_COOLDOWN:
            return None
        return max(1, math.ceil((MANUAL_COOLDOWN - elapsed).total_seconds()))

    def check_manual(self, ticket: Any, actor: Any) -> None:
        """Raise RatingRequestError for the first failing rule (order is the contract)."""
        now = utcnow_aware()
        if ticket.assigned_to is None:
            raise RatingRequestError("no_human_agent")
        if actor.role != UserRole.TENANT_ADMIN and actor.id != ticket.assigned_to:
            raise RatingRequestError("rating_request_forbidden")
        if not self._ratable(ticket, now):
            raise RatingRequestError("ticket_not_ratable")
        if self.agent_rating(ticket.session_id, ticket.assigned_to) is not None:
            raise RatingRequestError("already_rated")
        left = self._cooldown_left(ticket.session_id, ticket.assigned_to, now)
        if left is not None:
            raise RatingRequestError("rating_request_cooldown", retry_after_seconds=left)

    def evaluate(self, ticket: Any, actor: Any) -> Eligibility:
        open_req = (
            self.open_request(ticket.session_id, ticket.assigned_to) if ticket.assigned_to else None
        )
        try:
            self.check_manual(ticket, actor)
        except RatingRequestError as exc:
            return Eligibility(False, exc.code, exc.retry_after_seconds, open_req)
        return Eligibility(True, None, None, open_req)

    def rating_state(self, ticket: Any) -> tuple[str, UUID | None]:
        """`ticket_resolved` / `ticket_closed` fields: pending | already_rated | none."""
        if ticket.assigned_to is None:
            return "none", None
        if self.agent_rating(ticket.session_id, ticket.assigned_to) is not None:
            return "already_rated", None
        open_req = self.open_request(ticket.session_id, ticket.assigned_to)
        if open_req is not None:
            return "pending", open_req.id
        return "none", None

    # --- write -------------------------------------------------------------

    def create_request(
        self, ticket_id: UUID, *, source: str, requested_by: Any
    ) -> RatingRequestResult:
        """Mint (or, for auto, reuse) a request under a row lock on the ticket.

        A RatingRequestError leaves nothing written; the caller's session end releases
        the lock. Any other failure rolls back before re-raising.
        """
        if source not in RatingSource.REQUESTABLE:
            raise ValueError(f"unsupported rating request source: {source}")
        ticket = self.db.execute(
            select(Ticket)
            .where(Ticket.id == ticket_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()
        if ticket is None:
            raise ValueError("Ticket not found")
        try:
            if source == RatingSource.MANUAL_REQUEST:
                self.check_manual(ticket, requested_by)
            else:
                if ticket.assigned_to is None:
                    raise RatingRequestError("no_human_agent")
                if self.agent_rating(ticket.session_id, ticket.assigned_to) is not None:
                    raise RatingRequestError("already_rated")
            return self._mint(ticket, source=source, requested_by=requested_by)
        except RatingRequestError:
            raise
        except Exception:
            self.db.rollback()
            raise

    def _mint(self, ticket: Ticket, *, source: str, requested_by: Any) -> RatingRequestResult:
        agent_id = ticket.assigned_to
        agent = self.db.get(User, agent_id)
        full = agent.full_name if agent else None
        first = first_name(full)
        now = utcnow_aware()

        existing = self.open_request(ticket.session_id, agent_id)
        if existing is not None:
            if source == RatingSource.AUTO_RESOLVE:
                self.db.commit()  # nothing changed; releases the row lock
                return RatingRequestResult(existing, None, None, first, full, created=False)
            existing.expires_at = now  # manual after cooldown supersedes the open one

        token = new_token()
        request = RatingRequest(
            tenant_id=ticket.tenant_id,
            session_id=ticket.session_id,
            ticket_id=ticket.id,
            agent_id=agent_id,
            requested_by=requested_by.id if source == RatingSource.MANUAL_REQUEST else None,
            source=source,
            token_hash=hash_token(token),
            created_at=now,
            expires_at=now + REQUEST_TTL,
        )
        self.db.add(request)
        self.db.flush()
        # Persisted in the same transaction as the request (not via add_message, which
        # commits mid-lock): the request and its bound message exist together or not at all.
        message = Message(
            session_id=ticket.session_id,
            role=MessageRole.ASSISTANT,
            content=prompt_copy(first),
            meta={
                "system_notice": SYSTEM_NOTICE,
                "request_id": str(request.id),
                "agent_first_name": first,
                "rating_token": token,
            },
        )
        self.db.add(message)
        self.db.flush()
        request.message_id = message.id
        session = self.db.get(ConversationSession, ticket.session_id)
        if session is not None:
            session.last_message_at = utcnow_naive()
        self.db.commit()
        self.db.refresh(request)
        self.db.refresh(message)
        return RatingRequestResult(request, token, message, first, full, created=True)
```
(`logging`, `math`, `secrets`, `hashlib`, `Any`, `datetime` are all used above. Task 4 adds the `connection_manager` import together with the frame helpers that use it.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `poetry run pytest tests/rating/test_rating_request_service.py tests/rating/test_rating_request_concurrency.py -v`
Expected: service tests PASS; concurrency test SKIPPED (no Postgres `DATABASE_URL`). It runs for real in Task 10.

- [ ] **Step 5: Commit**

```bash
git add app/services/rating_request_service.py tests/rating/test_rating_request_service.py tests/rating/test_rating_request_concurrency.py
git commit -m "feat(ratings): RatingRequestService with row-locked mint, cooldown, supersede and reuse"
```

---

### Task 3: Staff redaction + ratings on conversation detail

**Review:** Opus (security: token must never reach a staff surface).

**Files:**
- Create: `app/utils/staff_redaction.py`, `app/schemas/rating.py`, `app/services/rating_read_service.py`
- Modify: `app/schemas/conversation.py:182-186` (`SessionDetailResponse`)
- Modify: `app/services/conversation_service.py:70-77` (`_to_session_response`)
- Modify: `app/api/v1/endpoints/chat.py:3045-3075` (`get_session`), `:3114-3148` (`get_session_messages`)
- Test: `tests/rating/test_staff_redaction.py`

**Interfaces:**
- Consumes: Task 2 `RatingRequestService.create_request`.
- Produces:
  - `app.utils.staff_redaction`: `STAFF_REDACTED_META_KEYS = frozenset({"rating_token"})`, `redact_staff_meta(meta: dict | None) -> dict | None` (returns the same object when nothing to drop; never mutates input), `redact_staff_messages(messages: list[MessageResponse]) -> list[MessageResponse]`.
  - `app.schemas.rating`: `SessionRatingBrief{id, score, comment, created_at, rated_party: "ai"|"human", rated_agent_id, rated_agent_name, source}` plus (used by later tasks) `RatingRequestOut`, `OpenRatingRequest`, `RatingStatusResponse`, `MyRatingItem`, `MyRatingsResponse`.
  - `app.services.rating_read_service`: `rated_party(rated_agent_id) -> str`, `agent_names(db, ids) -> dict[UUID, str]`, `ticket_refs(db, request_ids) -> dict[UUID, tuple[UUID, str]]`, `to_brief(rating, names) -> SessionRatingBrief`, `session_rating_briefs(db, session_id) -> list[SessionRatingBrief]` (oldest first).
  - `SessionDetailResponse.ratings: list[SessionRatingBrief]` (filled only by `GET /chat/sessions/{id}`).

- [ ] **Step 1: Write the failing tests**

`tests/rating/test_staff_redaction.py`:
```python
"""Spec §4.6: the rating token never reaches a staff-facing surface; customers keep it."""

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.api.deps import get_current_customer
from app.db.models.conversation import ConversationSession, Message, MessageRole
from app.db.models.review import RatingSource
from app.main import app
from app.schemas.customer_auth import CustomerUserResponse
from app.services.rating_request_service import RatingRequestService
from app.utils.datetime import utcnow_naive
from app.utils.staff_redaction import redact_staff_meta
from tests.api.v1.test_agent_websocket_auth import agent_ws_env  # noqa: F401 (fixture)
from tests.rating.conftest import headers, make_rating, make_ticket


def _requested(db, tenant, agent):
    t = make_ticket(db, tenant, assigned_to=agent.id)
    res = RatingRequestService(db).create_request(
        t.id, source=RatingSource.MANUAL_REQUEST, requested_by=agent
    )
    return t, res.token, res.request


def test_redact_staff_meta_unit():
    assert redact_staff_meta(None) is None
    assert redact_staff_meta({}) == {}
    plain = {"system_notice": "ticket_missed"}
    assert redact_staff_meta(plain) is plain
    src = {"system_notice": "rating_request", "request_id": "r", "rating_token": "t"}
    assert redact_staff_meta(src) == {"system_notice": "rating_request", "request_id": "r"}
    assert "rating_token" in src  # input not mutated


def test_admin_session_detail_and_message_list_have_no_token(client, db, tenant, agent, admin):
    t, token, req = _requested(db, tenant, agent)
    for path in (
        f"/api/v1/chat/sessions/{t.session_id}",
        f"/api/v1/chat/sessions/{t.session_id}/messages",
    ):
        r = client.get(path, headers=headers(admin))
        assert r.status_code == 200, r.text
        assert "rating_token" not in r.text
        assert token not in r.text
        assert str(req.id) in r.text  # the notice itself is still shown to staff


def test_agent_app_escalation_detail_has_no_token(client, db, tenant, agent):
    t, token, _ = _requested(db, tenant, agent)
    r = client.get(f"/api/v1/agent/escalations/{t.id}", headers=headers(agent))
    assert r.status_code == 200, r.text
    assert token not in r.text and "rating_token" not in r.text


def test_customer_history_keeps_token(client, db, tenant, agent):
    t, token, req = _requested(db, tenant, agent)
    session = db.get(ConversationSession, t.session_id)
    now = datetime.now(UTC)
    customer = CustomerUserResponse(
        id=uuid4(),
        full_name="Cus Tomer",
        email="customer@example.com",
        chat_user_key=session.user_identifier,
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    app.dependency_overrides[get_current_customer] = lambda: customer
    try:
        r = client.get(f"/api/v1/customer/conversations/{t.session_id}/messages")
    finally:
        app.dependency_overrides.pop(get_current_customer, None)
    assert r.status_code == 200, r.text
    notices = [
        m
        for m in r.json()["messages"]
        if (m.get("meta") or {}).get("system_notice") == "rating_request"
    ]
    assert notices and notices[0]["meta"]["rating_token"] == token
    assert notices[0]["meta"]["request_id"] == str(req.id)


def test_session_detail_lists_every_rating_oldest_first(client, db, tenant, agent, admin):
    t = make_ticket(db, tenant, assigned_to=agent.id)
    make_rating(
        db,
        session_id=t.session_id,
        tenant_id=tenant.id,
        score=3,
        created_at=utcnow_naive() - timedelta(hours=2),
    )
    make_rating(
        db,
        session_id=t.session_id,
        tenant_id=tenant.id,
        score=5,
        rated_agent_id=agent.id,
        source=RatingSource.MANUAL_REQUEST,
        comment="lovely",
        created_at=utcnow_naive() - timedelta(hours=1),
    )
    body = client.get(f"/api/v1/chat/sessions/{t.session_id}", headers=headers(admin)).json()
    assert [(r["rated_party"], r["score"]) for r in body["ratings"]] == [("ai", 3), ("human", 5)]
    human = body["ratings"][1]
    assert human["rated_agent_name"] == "Sam Rivers"
    assert human["source"] == "manual_request" and human["comment"] == "lovely"
    assert set(human) == {
        "id",
        "score",
        "comment",
        "created_at",
        "rated_party",
        "rated_agent_id",
        "rated_agent_name",
        "source",
    }


def test_agent_ws_replay_never_carries_token(agent_ws_env):  # noqa: F811
    s = agent_ws_env.session_factory()
    try:
        s.add(
            Message(
                session_id=agent_ws_env.session_id,
                role=MessageRole.ASSISTANT,
                content="How was your chat with Sam?",
                meta={
                    "system_notice": "rating_request",
                    "request_id": "r1",
                    "agent_first_name": "Sam",
                    "rating_token": "tok-secret-value",
                },
            )
        )
        s.add(Message(session_id=agent_ws_env.session_id, role=MessageRole.USER, content="hi"))
        s.commit()
    finally:
        s.close()
    frames = []
    url = f"{agent_ws_env.url}?token={agent_ws_env.valid_token()}"
    with agent_ws_env.client.websocket_connect(url) as ws:
        ws.send_json({"type": "ping"})
        while True:
            frame = ws.receive_json()
            frames.append(frame)
            if frame.get("type") == "pong":
                break
    dumped = json.dumps(frames)
    assert "tok-secret-value" not in dumped and "rating_token" not in dumped
    assert any(f.get("type") == "customer_message" for f in frames)
```

- [ ] **Step 2: Run to verify failure**

Run: `poetry run pytest tests/rating/test_staff_redaction.py -v`
Expected: FAIL (`ModuleNotFoundError: app.utils.staff_redaction`). The WS replay test may already pass (the replay sends only USER/HUMAN_AGENT); it stays as a pin.

- [ ] **Step 3: Implement**

`app/utils/staff_redaction.py`:
```python
"""Strip customer-only secrets from message meta before it reaches staff (spec §4.6)."""

from app.schemas.conversation import MessageResponse

STAFF_REDACTED_META_KEYS = frozenset({"rating_token"})


def redact_staff_meta(meta: dict | None) -> dict | None:
    if not meta or not any(key in meta for key in STAFF_REDACTED_META_KEYS):
        return meta
    return {k: v for k, v in meta.items() if k not in STAFF_REDACTED_META_KEYS}


def redact_staff_messages(messages: list[MessageResponse]) -> list[MessageResponse]:
    return [m.model_copy(update={"meta": redact_staff_meta(m.meta)}) for m in messages]
```

`app/schemas/rating.py`:
```python
"""N4 agent-rating schemas (spec §4.3)."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class SessionRatingBrief(BaseModel):
    """One rating as shown on conversation/ticket detail (staff)."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    score: int
    comment: str | None = None
    created_at: datetime
    rated_party: str = "ai"
    rated_agent_id: UUID | None = None
    rated_agent_name: str | None = None
    source: str = "idle_prompt"


class RatingRequestOut(BaseModel):
    """201 body of POST /tickets/{id}/rating-requests. Never carries the token."""

    request_id: UUID
    ticket_id: UUID | None
    session_id: UUID
    agent_id: UUID
    agent_name: str | None
    source: str
    message_id: UUID | None
    created_at: datetime
    expires_at: datetime


class OpenRatingRequest(BaseModel):
    request_id: UUID
    source: str
    created_at: datetime
    expires_at: datetime


class RatingStatusResponse(BaseModel):
    can_request: bool
    reason_code: str | None = None
    retry_after_seconds: int | None = None
    open_request: OpenRatingRequest | None = None
    rating: SessionRatingBrief | None = None


class MyRatingItem(BaseModel):
    id: UUID
    score: int
    comment: str | None = None
    created_at: datetime
    ticket_number: str | None = None
    source: str


class MyRatingsResponse(BaseModel):
    period_start: datetime
    period_end: datetime
    count: int
    average_score: float | None = None
    csat_percent: float | None = None
    recent: list[MyRatingItem] = Field(default_factory=list)
```

`app/services/rating_read_service.py`:
```python
"""Read-side helpers for N4 ratings: names, ticket refs, briefs (stats added in Task 8)."""

from collections.abc import Iterable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.conversation import Ticket
from app.db.models.review import RatingRequest, SessionRating
from app.db.models.user import User
from app.schemas.rating import SessionRatingBrief


def rated_party(rated_agent_id: UUID | None) -> str:
    return "human" if rated_agent_id else "ai"


def agent_names(db: Session, ids: Iterable[UUID | None]) -> dict[UUID, str]:
    wanted = {i for i in ids if i}
    if not wanted:
        return {}
    users = db.execute(select(User).where(User.id.in_(wanted))).scalars()
    return {u.id: u.full_name for u in users}


def ticket_refs(db: Session, request_ids: Iterable[UUID | None]) -> dict[UUID, tuple[UUID, str]]:
    wanted = {i for i in request_ids if i}
    if not wanted:
        return {}
    rows = db.execute(
        select(RatingRequest.id, Ticket.id, Ticket.ticket_number)
        .join(Ticket, Ticket.id == RatingRequest.ticket_id)
        .where(RatingRequest.id.in_(wanted))
    ).all()
    return {rid: (tid, number) for rid, tid, number in rows}


def to_brief(rating: SessionRating, names: dict[UUID, str]) -> SessionRatingBrief:
    return SessionRatingBrief(
        id=rating.id,
        score=rating.score,
        comment=rating.comment,
        created_at=rating.created_at,
        rated_party=rated_party(rating.rated_agent_id),
        rated_agent_id=rating.rated_agent_id,
        rated_agent_name=names.get(rating.rated_agent_id) if rating.rated_agent_id else None,
        source=rating.source,
    )


def session_rating_briefs(db: Session, session_id: UUID) -> list[SessionRatingBrief]:
    rows = (
        db.execute(
            select(SessionRating)
            .where(SessionRating.session_id == session_id)
            .order_by(SessionRating.created_at)
        )
        .scalars()
        .all()
    )
    names = agent_names(db, (r.rated_agent_id for r in rows))
    return [to_brief(r, names) for r in rows]
```

`app/schemas/conversation.py` — add `from app.schemas.rating import SessionRatingBrief` to the imports and extend `SessionDetailResponse` (lines 182-186):
```python
class SessionDetailResponse(SessionResponse):
    """Schema for session with messages."""

    messages: list[MessageResponse] = Field(default_factory=list)
    ticket_id: UUID | None = None  # latest ticket for this session
    ratings: list[SessionRatingBrief] = Field(
        default_factory=list,
        description="Every rating for the session (AI and human), oldest first; staff detail only",
    )
```

`app/services/conversation_service.py` `_to_session_response` — right after `response.ticket_id = …` (line ~77) add:
```python
            # The ORM backref would auto-fill this; the admin endpoint fills it explicitly.
            response.ratings = []
```

`app/api/v1/endpoints/chat.py` — imports: `from app.services.rating_read_service import session_rating_briefs` and `from app.utils.staff_redaction import redact_staff_messages`. In `get_session` replace the final `return session` (line ~3075) with:
```python
    session.messages = redact_staff_messages(session.messages)
    session.ratings = session_rating_briefs(db, session_id)
    return session
```
In `get_session_messages` replace `return messages` (line ~3148) with:
```python
    return redact_staff_messages(messages)
```
Do **not** touch `visitor_service.py:517` (the public visitor bootstrap — customer-facing, see Self-Review "spec conflicts") or `customer_conversations.py`.

- [ ] **Step 4: Run tests**

Run: `poetry run pytest tests/rating/test_staff_redaction.py tests/api/v1/test_agent_websocket_auth.py tests/api/v1/test_customer_conversations.py tests/agent_caller/test_missed_ticket_lifecycle.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/utils/staff_redaction.py app/schemas/rating.py app/services/rating_read_service.py app/schemas/conversation.py app/services/conversation_service.py app/api/v1/endpoints/chat.py tests/rating/test_staff_redaction.py
git commit -m "feat(ratings): redact rating tokens from staff message surfaces; ratings on conversation detail"
```

---

### Task 4: Manual request + rating status endpoints, WS frames

**Review:** Opus (permission: who may request, who may see a rating's score/comment).

**Files:**
- Modify: `app/services/rating_request_service.py` (append frame helpers + `fanout_rating_request`)
- Create: `app/api/v1/endpoints/ticket_ratings.py`
- Modify: `app/api/v1/api.py:3-40` (import), `:276` (mount after `tickets.router`)
- Test: `tests/rating/test_rating_request_endpoints.py`

**Interfaces:**
- Consumes: Task 2 service, Task 3 `RatingRequestOut`, `OpenRatingRequest`, `RatingStatusResponse`, `agent_names`, `to_brief`.
- Produces: `customer_frame(result) -> dict` (`rating_requested`: `{type, request_id, token, agent_name /* first name or null */, message_id, content, source, timestamp, expires_at}`), `agent_frame(request) -> dict` (`rating_request_created`: `{type, request_id, ticket_id, agent_id, source, created_at}`), `async fanout_rating_request(result) -> None` (no-op unless `result.created`; never raises). Routes `POST /api/v1/tickets/{ticket_id}/rating-requests` (201 `RatingRequestOut`) and `GET /api/v1/tickets/{ticket_id}/rating-status` (200 `RatingStatusResponse`).

- [ ] **Step 1: Write the failing tests**

`tests/rating/test_rating_request_endpoints.py`:
```python
"""POST /tickets/{id}/rating-requests and GET /tickets/{id}/rating-status (spec §4.3.1-2)."""

from datetime import timedelta
from uuid import UUID

import pytest

from app.db.models.conversation import TicketStatus
from app.db.models.review import RatingRequest, RatingSource
from app.services.rating_request_service import RatingRequestService, hash_token
from app.utils.datetime import utcnow_aware
from tests.rating.conftest import headers, make_rating, make_ticket

KEYS_201 = {
    "request_id",
    "ticket_id",
    "session_id",
    "agent_id",
    "agent_name",
    "source",
    "message_id",
    "created_at",
    "expires_at",
}


def _post(client, ticket, user):
    return client.post(f"/api/v1/tickets/{ticket.id}/rating-requests", headers=headers(user))


def _status(client, ticket, user):
    return client.get(f"/api/v1/tickets/{ticket.id}/rating-status", headers=headers(user))


def test_assignee_request_201_and_frames(client, db, tenant, agent, fanout):
    t = make_ticket(db, tenant, assigned_to=agent.id)
    r = _post(client, t, agent)
    assert r.status_code == 201, r.text
    body = r.json()
    assert set(body) == KEYS_201
    assert (body["agent_name"], body["source"]) == ("Sam Rivers", "manual_request")
    assert "token" not in r.text

    calls = [c.kwargs for c in fanout.await_args_list]
    assert [c["message"]["type"] for c in calls] == ["rating_requested", "rating_request_created"]
    customer, agents = calls
    assert (customer["to_customer"], customer["to_agents"]) == (True, False)
    assert (agents["to_customer"], agents["to_agents"]) == (False, True)
    frame = customer["message"]
    assert set(frame) == {
        "type",
        "request_id",
        "token",
        "agent_name",
        "message_id",
        "content",
        "source",
        "timestamp",
        "expires_at",
    }
    assert frame["agent_name"] == "Sam" and frame["content"] == "How was your chat with Sam?"
    req = db.get(RatingRequest, UUID(body["request_id"]))
    assert hash_token(frame["token"]) == req.token_hash
    assert set(agents["message"]) == {
        "type",
        "request_id",
        "ticket_id",
        "agent_id",
        "source",
        "created_at",
    }


def test_admin_may_request_for_assignee(client, db, tenant, agent, admin):
    t = make_ticket(db, tenant, assigned_to=agent.id)
    r = _post(client, t, admin)
    assert r.status_code == 201 and r.json()["agent_id"] == str(agent.id)


@pytest.mark.parametrize("who", ["other_agent", "viewer"])
def test_non_assignee_and_viewer_403(client, db, tenant, agent, request, who):
    t = make_ticket(db, tenant, assigned_to=agent.id)
    r = _post(client, t, request.getfixturevalue(who))
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "rating_request_forbidden"


def test_other_tenant_ticket_is_404_string(client, db, other_tenant, agent):
    t = make_ticket(db, other_tenant)
    for r in (_post(client, t, agent), _status(client, t, agent)):
        assert r.status_code == 404 and r.json()["detail"] == "Ticket not found"


def test_unassigned_409_no_human_agent(client, db, tenant, admin):
    t = make_ticket(db, tenant, status=TicketStatus.OPEN)
    r = _post(client, t, admin)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "no_human_agent"


def test_open_ticket_409_not_ratable(client, db, tenant, agent):
    t = make_ticket(db, tenant, status=TicketStatus.OPEN, assigned_to=agent.id)
    r = _post(client, t, agent)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "ticket_not_ratable"


def test_second_request_inside_10_min_409_cooldown(client, db, tenant, agent):
    t = make_ticket(db, tenant, assigned_to=agent.id)
    assert _post(client, t, agent).status_code == 201
    r = _post(client, t, agent)
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "rating_request_cooldown"
    assert 0 < detail["retry_after_seconds"] <= 600


def test_already_rated_409(client, db, tenant, agent):
    t = make_ticket(db, tenant, assigned_to=agent.id)
    make_rating(db, session_id=t.session_id, tenant_id=tenant.id, rated_agent_id=agent.id)
    r = _post(client, t, agent)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "already_rated"


def test_status_can_request_then_cooldown_with_open_request(client, db, tenant, agent):
    t = make_ticket(db, tenant, assigned_to=agent.id)
    first = _status(client, t, agent).json()
    assert first == {
        "can_request": True,
        "reason_code": None,
        "retry_after_seconds": None,
        "open_request": None,
        "rating": None,
    }
    req_id = _post(client, t, agent).json()["request_id"]
    after = _status(client, t, agent)
    assert "token" not in after.text
    body = after.json()
    assert (body["can_request"], body["reason_code"]) == (False, "rating_request_cooldown")
    assert body["open_request"]["request_id"] == req_id
    assert body["open_request"]["source"] == "manual_request"


def test_status_rating_visible_only_to_admin_and_rated_agent(
    client, db, tenant, agent, other_agent, admin
):
    t = make_ticket(db, tenant, assigned_to=agent.id)
    make_rating(
        db,
        session_id=t.session_id,
        tenant_id=tenant.id,
        rated_agent_id=agent.id,
        score=2,
        comment="slow",
        source=RatingSource.MANUAL_REQUEST,
    )
    for user in (admin, agent):
        rating = _status(client, t, user).json()["rating"]
        assert rating["score"] == 2 and rating["comment"] == "slow"
        assert rating["rated_agent_name"] == "Sam Rivers" and rating["rated_party"] == "human"
    hidden = _status(client, t, other_agent).json()
    assert hidden["rating"] is None
    assert hidden["reason_code"] == "rating_request_forbidden"


def test_status_viewer_403(client, db, tenant, agent, viewer):
    t = make_ticket(db, tenant, assigned_to=agent.id)
    r = _status(client, t, viewer)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "rating_request_forbidden"


def test_fanout_failure_does_not_fail_request(client, db, tenant, agent, fanout):
    fanout.side_effect = RuntimeError("redis down")
    t = make_ticket(db, tenant, assigned_to=agent.id)
    assert _post(client, t, agent).status_code == 201


def test_supersede_via_endpoint_after_cooldown(client, db, tenant, agent):
    t = make_ticket(db, tenant, assigned_to=agent.id)
    old = RatingRequestService(db).create_request(
        t.id, source=RatingSource.MANUAL_REQUEST, requested_by=agent
    ).request
    old.created_at = utcnow_aware() - timedelta(minutes=11)
    db.commit()
    r = _post(client, t, agent)
    assert r.status_code == 201 and r.json()["request_id"] != str(old.id)
```

- [ ] **Step 2: Run to verify failure**

Run: `poetry run pytest tests/rating/test_rating_request_endpoints.py -v`
Expected: FAIL (404 on the new routes).

- [ ] **Step 3: Implement**

Add `from app.core.websocket import connection_manager` to the imports of `app/services/rating_request_service.py`, then append:
```python
# --- WS frames (spec §4.5) --------------------------------------------------


def customer_frame(result: RatingRequestResult) -> dict[str, Any]:
    req, msg = result.request, result.message
    return {
        "type": "rating_requested",
        "request_id": str(req.id),
        "token": result.token,
        "agent_name": result.agent_first_name,
        "message_id": str(msg.id),
        "content": msg.content,
        "source": req.source,
        "timestamp": ensure_utc_aware(msg.created_at).isoformat(),
        "expires_at": ensure_utc_aware(req.expires_at).isoformat(),
    }


def agent_frame(req: RatingRequest) -> dict[str, Any]:
    return {
        "type": "rating_request_created",
        "request_id": str(req.id),
        "ticket_id": str(req.ticket_id) if req.ticket_id else None,
        "agent_id": str(req.agent_id),
        "source": req.source,
        "created_at": ensure_utc_aware(req.created_at).isoformat(),
    }


async def fanout_rating_request(result: RatingRequestResult) -> None:
    """Customer-only bound card + token-free agent notice. Best effort: already committed."""
    if not result.created or result.token is None or result.message is None:
        return
    req = result.request
    for message, to_customer in ((customer_frame(result), True), (agent_frame(req), False)):
        try:
            await connection_manager.fanout_event(
                session_id=req.session_id,
                message=message,
                to_customer=to_customer,
                to_agents=not to_customer,
            )
        except Exception as exc:  # noqa: BLE001 — never fail the caller; never log the payload
            logger.error(
                "rating request fanout failed request_id=%s frame=%s (%s)",
                req.id,
                message["type"],
                type(exc).__name__,
            )
```

`app/api/v1/endpoints/ticket_ratings.py`:
```python
"""Agent-requested ratings on a ticket (spec §4.3.1, §4.3.2). Mounted under /tickets."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.db.models.conversation import Ticket
from app.db.models.review import RatingSource
from app.db.models.user import UserRole
from app.schemas.rating import OpenRatingRequest, RatingRequestOut, RatingStatusResponse
from app.schemas.user import UserResponse
from app.services.rating_read_service import agent_names, to_brief
from app.services.rating_request_service import (
    RatingRequestError,
    RatingRequestService,
    fanout_rating_request,
)

router = APIRouter()

_STAFF = (UserRole.TENANT_ADMIN, UserRole.SUPPORT_AGENT)


def _load_ticket(db: Session, ticket_id: UUID, user: UserResponse) -> Ticket:
    ticket = db.get(Ticket, ticket_id)
    if ticket is None or ticket.tenant_id != user.tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ticket not found")
    if user.role not in _STAFF:
        err = RatingRequestError("rating_request_forbidden")
        raise HTTPException(status_code=err.status_code, detail=err.detail())
    return ticket


@router.post(
    "/{ticket_id}/rating-requests",
    response_model=RatingRequestOut,
    status_code=status.HTTP_201_CREATED,
)
async def request_rating(
    ticket_id: UUID,
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_user),
):
    """Ask the customer to rate the ticket's assignee. The response never carries the token."""
    _load_ticket(db, ticket_id, current_user)
    try:
        result = RatingRequestService(db).create_request(
            ticket_id, source=RatingSource.MANUAL_REQUEST, requested_by=current_user
        )
    except RatingRequestError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail()) from exc
    await fanout_rating_request(result)
    req = result.request
    return RatingRequestOut(
        request_id=req.id,
        ticket_id=req.ticket_id,
        session_id=req.session_id,
        agent_id=req.agent_id,
        agent_name=result.agent_name,
        source=req.source,
        message_id=req.message_id,
        created_at=req.created_at,
        expires_at=req.expires_at,
    )


@router.get("/{ticket_id}/rating-status", response_model=RatingStatusResponse)
def rating_status(
    ticket_id: UUID,
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_user),
):
    """Drives the Request-rating button and the ticket rating badge.

    `rating` is the human rating for (session, current assignee), shown only to tenant
    admins and to the rated agent (N4-D5); everyone else gets null.
    """
    ticket = _load_ticket(db, ticket_id, current_user)
    svc = RatingRequestService(db)
    elig = svc.evaluate(ticket, current_user)
    rating = None
    if ticket.assigned_to is not None:
        row = svc.agent_rating(ticket.session_id, ticket.assigned_to)
        if row is not None and (
            current_user.role == UserRole.TENANT_ADMIN or row.rated_agent_id == current_user.id
        ):
            rating = to_brief(row, agent_names(db, [row.rated_agent_id]))
    open_req = elig.open_request
    return RatingStatusResponse(
        can_request=elig.can_request,
        reason_code=elig.reason_code,
        retry_after_seconds=elig.retry_after_seconds,
        open_request=(
            OpenRatingRequest(
                request_id=open_req.id,
                source=open_req.source,
                created_at=open_req.created_at,
                expires_at=open_req.expires_at,
            )
            if open_req is not None
            else None
        ),
        rating=rating,
    )
```

`app/api/v1/api.py`: add `ticket_ratings,` to the endpoint import list (alphabetical, after `tenant_channels,`), and directly under the tickets mount (line 276):
```python
api_router.include_router(ticket_ratings.router, prefix="/tickets", tags=["tickets"])
```

- [ ] **Step 4: Run tests**

Run: `poetry run pytest tests/rating/test_rating_request_endpoints.py tests/rating/test_rating_request_service.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/rating_request_service.py app/api/v1/endpoints/ticket_ratings.py app/api/v1/api.py tests/rating/test_rating_request_endpoints.py
git commit -m "feat(ratings): manual rating-request and rating-status endpoints with customer/agent frames"
```

---

### Task 5: Auto request on resolve (ordered before `ticket_resolved`) + close frame fields

**Files:**
- Modify: `app/services/rating_request_service.py` (append `auto_request_on_resolve`, `rating_frame_fields`)
- Modify: `app/api/v1/endpoints/tickets.py:16-28` (imports), `:288-312` (resolve), `:348-366` (close)
- Modify: `app/schemas/ticket.py:79-95` (`TicketResponse.rating_request_id`)
- Test: `tests/rating/test_resolve_hook.py`

**Interfaces:**
- Consumes: Task 2 `create_request`, `rating_state`; Task 4 `fanout_rating_request`.
- Produces: `async auto_request_on_resolve(db, ticket_id: UUID, assigned_to: UUID | None) -> tuple[UUID | None, str]` (never raises; returns `(rating_request_id, rating_state)`); `rating_frame_fields(db, ticket) -> dict` (`{"rating_request_id": str | None, "rating_state": str}`, never raises); `TicketResponse.rating_request_id: UUID | None = None`. `ticket_resolved` and `ticket_closed` frames gain `rating_request_id` + `rating_state`.

- [ ] **Step 1: Write the failing tests**

`tests/rating/test_resolve_hook.py`:
```python
"""Resolve hook (spec §4.4): request before ticket_resolved; close carries fields."""

import inspect
from unittest.mock import patch

from sqlalchemy import func, select

import app.workers.conversation_auto_resolver as auto_resolver
from app.db.models.conversation import Ticket, TicketStatus
from app.db.models.review import RatingRequest, RatingSource
from app.services.rating_request_service import RatingRequestService
from tests.rating.conftest import headers, make_rating, make_ticket


def _resolve(client, ticket, user):
    return client.post(
        f"/api/v1/tickets/{ticket.id}/resolve",
        json={"resolution_notes": "Fixed"},
        headers=headers(user),
    )


def _types(fanout):
    return [c.kwargs["message"]["type"] for c in fanout.await_args_list]


def _requests(db, ticket):
    return db.execute(
        select(func.count(RatingRequest.id)).where(RatingRequest.session_id == ticket.session_id)
    ).scalar_one()


def test_resolve_with_assignee_requests_rating_before_ticket_resolved(
    client, db, tenant, agent, fanout
):
    t = make_ticket(db, tenant, assigned_to=agent.id)
    r = _resolve(client, t, agent)
    assert r.status_code == 200, r.text
    assert _types(fanout) == ["rating_requested", "rating_request_created", "ticket_resolved"]
    resolved = fanout.await_args_list[-1].kwargs["message"]
    req = db.execute(select(RatingRequest).where(RatingRequest.ticket_id == t.id)).scalar_one()
    assert (req.source, req.requested_by) == (RatingSource.AUTO_RESOLVE, None)
    assert resolved["rating_state"] == "pending"
    assert resolved["rating_request_id"] == str(req.id)
    assert r.json()["rating_request_id"] == str(req.id)


def test_resolve_reuses_open_manual_request_without_new_card(client, db, tenant, agent, fanout):
    t = make_ticket(db, tenant, assigned_to=agent.id)
    manual = RatingRequestService(db).create_request(
        t.id, source=RatingSource.MANUAL_REQUEST, requested_by=agent
    )
    fanout.reset_mock()
    r = _resolve(client, t, agent)
    assert r.status_code == 200
    assert _types(fanout) == ["ticket_resolved"]
    resolved = fanout.await_args_list[-1].kwargs["message"]
    assert (resolved["rating_state"], resolved["rating_request_id"]) == (
        "pending",
        str(manual.request.id),
    )
    assert _requests(db, t) == 1


def test_admin_resolving_unassigned_missed_ticket_is_none(client, db, tenant, admin, fanout):
    t = make_ticket(db, tenant, status=TicketStatus.MISSED, assigned_to=None)
    r = _resolve(client, t, admin)
    assert r.status_code == 200
    assert _types(fanout) == ["ticket_resolved"]
    resolved = fanout.await_args_list[-1].kwargs["message"]
    assert (resolved["rating_state"], resolved["rating_request_id"]) == ("none", None)
    assert r.json()["rating_request_id"] is None
    assert _requests(db, t) == 0


def test_resolve_when_agent_already_rated(client, db, tenant, agent, fanout):
    t = make_ticket(db, tenant, assigned_to=agent.id)
    make_rating(db, session_id=t.session_id, tenant_id=tenant.id, rated_agent_id=agent.id)
    r = _resolve(client, t, agent)
    assert r.status_code == 200
    resolved = fanout.await_args_list[-1].kwargs["message"]
    assert (resolved["rating_state"], resolved["rating_request_id"]) == ("already_rated", None)
    assert _requests(db, t) == 0


def test_request_failure_never_fails_resolve(client, db, tenant, agent, fanout):
    t = make_ticket(db, tenant, assigned_to=agent.id)
    with patch(
        "app.services.rating_request_service.RatingRequestService.create_request",
        side_effect=RuntimeError("db hiccup"),
    ):
        r = _resolve(client, t, agent)
    assert r.status_code == 200, r.text
    assert db.get(Ticket, t.id).status == TicketStatus.RESOLVED
    assert _types(fanout) == ["ticket_resolved"]
    assert fanout.await_args_list[-1].kwargs["message"]["rating_state"] == "none"


def test_close_carries_rating_fields_without_creating(client, db, tenant, agent, admin, fanout):
    t = make_ticket(db, tenant, assigned_to=agent.id)
    req = RatingRequestService(db).create_request(
        t.id, source=RatingSource.MANUAL_REQUEST, requested_by=agent
    ).request
    fanout.reset_mock()
    r = client.post(f"/api/v1/tickets/{t.id}/close", headers=headers(admin))
    assert r.status_code == 200
    closed = fanout.await_args_list[-1].kwargs["message"]
    assert closed["type"] == "ticket_closed"
    assert (closed["rating_state"], closed["rating_request_id"]) == ("pending", str(req.id))
    assert _requests(db, t) == 1


def test_close_unassigned_is_none(client, db, tenant, admin, fanout):
    t = make_ticket(db, tenant, status=TicketStatus.OPEN)
    client.post(f"/api/v1/tickets/{t.id}/close", headers=headers(admin))
    closed = fanout.await_args_list[-1].kwargs["message"]
    assert (closed["rating_state"], closed["rating_request_id"]) == ("none", None)


def test_auto_resolver_never_requests_ratings():
    assert "rating_request" not in inspect.getsource(auto_resolver)
```

- [ ] **Step 2: Run to verify failure**

Run: `poetry run pytest tests/rating/test_resolve_hook.py -v`
Expected: FAIL (`KeyError: 'rating_state'` / frame list lacks `rating_requested`).

- [ ] **Step 3: Implement**

Append to `app/services/rating_request_service.py`:
```python
# --- resolve / close hooks (spec §4.4) -------------------------------------


def _state_or_none(db: Session, ticket_id: UUID) -> tuple[UUID | None, str]:
    try:
        ticket = db.get(Ticket, ticket_id)
        if ticket is None:
            return None, "none"
        state, request_id = RatingRequestService(db).rating_state(ticket)
        return request_id, state
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.error("rating state lookup failed ticket_id=%s (%s)", ticket_id, type(exc).__name__)
        return None, "none"


async def auto_request_on_resolve(
    db: Session, ticket_id: UUID, assigned_to: UUID | None
) -> tuple[UUID | None, str]:
    """Create/reuse the auto request and fan it out BEFORE the caller sends ticket_resolved.

    Never raises: a rating failure must never fail the resolve.
    """
    if assigned_to is None:
        return None, "none"
    try:
        result = RatingRequestService(db).create_request(
            ticket_id, source=RatingSource.AUTO_RESOLVE, requested_by=None
        )
    except RatingRequestError as exc:
        if exc.code == "already_rated":
            return None, "already_rated"
        return _state_or_none(db, ticket_id)
    except Exception as exc:  # noqa: BLE001
        logger.error("auto rating request failed ticket_id=%s (%s)", ticket_id, type(exc).__name__)
        return _state_or_none(db, ticket_id)
    await fanout_rating_request(result)  # no-op on reuse (token is not recoverable server-side)
    return result.request.id, "pending"


def rating_frame_fields(db: Session, ticket: Any) -> dict[str, Any]:
    """`rating_request_id` + `rating_state` for ticket_closed; never raises."""
    request_id, state = _state_or_none(db, ticket.id)
    return {"rating_request_id": str(request_id) if request_id else None, "rating_state": state}
```

`app/schemas/ticket.py` — in `TicketResponse` after `resolved_at` (line 93):
```python
    rating_request_id: UUID | None = None  # set only on the resolve response (N4 §4.4)
```

`app/api/v1/endpoints/tickets.py` — add import:
```python
from app.services.rating_request_service import auto_request_on_resolve, rating_frame_fields
```
Replace the body of the `try:` in `resolve_ticket` (lines ~288-312) with:
```python
    try:
        resolved_ticket = ticket_service.resolve_ticket(ticket_id, resolve_data)

        # N4 §4.4: the auto rating request (and its customer card) must be in the widget's
        # state before ticket_resolved, because the widget clears its session on that frame.
        rating_request_id, rating_state = await auto_request_on_resolve(
            db, resolved_ticket.id, resolved_ticket.assigned_to
        )
        resolved_ticket.rating_request_id = rating_request_id

        # Broadcast ticket_resolved event via WebSocket
        if resolved_ticket.session_id:
            await connection_manager.fanout_event(
                session_id=resolved_ticket.session_id,
                message={
                    "type": "ticket_resolved",
                    "ticket_id": str(resolved_ticket.id),
                    "ticket_number": resolved_ticket.ticket_number,
                    "resolution_notes": resolved_ticket.resolution_notes,
                    "resolved_by": current_user.full_name,
                    "resolved_at": (
                        resolved_ticket.resolved_at.isoformat()
                        if resolved_ticket.resolved_at
                        else None
                    ),
                    "rating_request_id": str(rating_request_id) if rating_request_id else None,
                    "rating_state": rating_state,
                },
                to_customer=True,
                to_agents=True,
            )

        return resolved_ticket
```
In `close_ticket`, add the two fields to the `ticket_closed` message dict (after `"closed_at": …`):
```python
                    **rating_frame_fields(db, closed_ticket),
```
Leave `app/workers/conversation_auto_resolver.py` untouched.

- [ ] **Step 4: Run tests**

Run: `poetry run pytest tests/rating/test_resolve_hook.py tests/agent_caller/test_missed_ticket_lifecycle.py tests/agent_caller -k "resolve or close" -v`
Expected: PASS (existing resolve tests unaffected; they mock `fanout_event`).

- [ ] **Step 5: Commit**

```bash
git add app/services/rating_request_service.py app/api/v1/endpoints/tickets.py app/schemas/ticket.py tests/rating/test_resolve_hook.py
git commit -m "feat(ratings): auto rating request on resolve, ordered before ticket_resolved; rating_state on resolve/close"
```

---

### Task 6: Token-gated public `POST /reviews/session`

**Review:** Opus (security: unauthenticated endpoint that writes an attributed rating).

**Files:**
- Modify: `app/schemas/review.py:13-51`
- Modify: `app/services/review_service.py:1-79` (imports, `create_session_rating`)
- Modify: `app/api/v1/endpoints/reviews.py:1-95` (imports, POST handler)
- Modify: `app/utils/http_rate_limiter.py` (after `video_register_rate_limiter` ~:73, and after `chat_video_register_rate_limit` ~:258)
- Test: `tests/rating/test_public_rating_submit.py`

**Interfaces:**
- Consumes: Task 1 models; Task 2 `hash_token`.
- Produces:
  - `SessionRatingCreate.rating_request_token: str | None` (1–128 chars), `comment` ≤ 500.
  - `SessionRatingSubmitResponse` = previous `SessionRatingResponse` fields + `rated_agent_id`, `source`, `rating_request_id`, computed `rated_party` (`"human"|"ai"`). `SessionRatingResponse(SessionRatingSubmitResponse)` adds `rated_agent_name`, `ticket_id`, `ticket_number` (used by Task 7). `SessionRatingListResponse.csat_percent: float | None`.
  - `app.services.review_service`: `class RatingSubmitError(Exception)` (`.code`, `.status_code`, `.detail()`), `legacy_source(meta: dict | None) -> str`.
  - `app.utils.http_rate_limiter`: `rating_rate_limiter` (`http_rating`, 20/3600), `async rating_rate_limit(request)` (429 → `{"code": "rating_rate_limited", …}`).

- [ ] **Step 1: Write the failing tests**

`tests/rating/test_public_rating_submit.py`:
```python
"""Public POST /reviews/session: legacy path unchanged, token path attributes (spec §4.3.3)."""

from datetime import timedelta
from unittest.mock import AsyncMock, patch

from app.db.models.conversation import ConversationSession
from app.db.models.review import RatingRequest, RatingSource
from app.services.rating_request_service import RatingRequestService
from app.utils.datetime import utcnow_aware
from app.utils.http_rate_limiter import rating_rate_limiter
from tests.rating.conftest import make_ticket

URL = "/api/v1/reviews/session"


def _ident(db, ticket):
    return db.get(ConversationSession, ticket.session_id).user_identifier


def _request(db, ticket, actor):
    return RatingRequestService(db).create_request(
        ticket.id, source=RatingSource.MANUAL_REQUEST, requested_by=actor
    )


def _body(db, ticket, **kw):
    return {"session_id": str(ticket.session_id), "user_identifier": _ident(db, ticket), "score": 5, **kw}


def test_legacy_untokened_rating_unchanged(client, db, tenant):
    t = make_ticket(db, tenant)
    r = client.post(URL, json=_body(db, t, meta={"source": "agent_resolved"}))
    assert r.status_code == 201, r.text
    body = r.json()
    assert (body["rated_party"], body["rated_agent_id"], body["source"]) == (
        "ai",
        None,
        "resolution_prompt",
    )
    assert body["rating_request_id"] is None
    dup = client.post(URL, json=_body(db, t))
    assert dup.status_code == 409
    assert dup.json()["detail"] == "Rating already exists for this session"


def test_legacy_default_source_is_idle_prompt(client, db, tenant):
    t = make_ticket(db, tenant)
    assert client.post(URL, json=_body(db, t)).json()["source"] == "idle_prompt"


def test_legacy_string_errors_unchanged(client, db, tenant):
    t = make_ticket(db, tenant)
    missing = client.post(URL, json={**_body(db, t), "session_id": str(tenant.id)})
    assert missing.status_code == 400 and missing.json()["detail"] == "Session not found"
    wrong = client.post(URL, json={**_body(db, t), "user_identifier": "someone-else"})
    assert wrong.status_code == 400
    assert wrong.json()["detail"] == "User identifier does not match session"


def test_token_happy_path_attributes_and_fulfils(client, db, tenant, agent):
    t = make_ticket(db, tenant, assigned_to=agent.id)
    res = _request(db, t, agent)
    r = client.post(URL, json=_body(db, t, rating_request_token=res.token, comment="great"))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["rated_agent_id"] == str(agent.id)
    assert (body["rated_party"], body["source"]) == ("human", "manual_request")
    assert body["rating_request_id"] == str(res.request.id)
    db.expire_all()
    assert db.get(RatingRequest, res.request.id).fulfilled_at is not None


def test_client_sent_rated_agent_id_is_ignored(client, db, tenant, agent, other_agent):
    legacy = make_ticket(db, tenant)
    r1 = client.post(URL, json=_body(db, legacy, rated_agent_id=str(agent.id)))
    assert r1.status_code == 201 and r1.json()["rated_agent_id"] is None
    t = make_ticket(db, tenant, assigned_to=agent.id)
    res = _request(db, t, agent)
    r2 = client.post(
        URL, json=_body(db, t, rating_request_token=res.token, rated_agent_id=str(other_agent.id))
    )
    assert r2.json()["rated_agent_id"] == str(agent.id)


def test_token_rating_attributes_to_request_agent_after_reassignment(
    client, db, tenant, agent, other_agent
):
    t = make_ticket(db, tenant, assigned_to=agent.id)
    res = _request(db, t, agent)
    t.assigned_to = other_agent.id
    db.commit()
    r = client.post(URL, json=_body(db, t, rating_request_token=res.token))
    assert r.status_code == 201 and r.json()["rated_agent_id"] == str(agent.id)


def test_unknown_token_400(client, db, tenant):
    t = make_ticket(db, tenant)
    r = client.post(URL, json=_body(db, t, rating_request_token="not-a-real-token"))
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "rating_request_invalid"


def test_token_from_another_session_400(client, db, tenant, agent):
    a = make_ticket(db, tenant, assigned_to=agent.id)
    b = make_ticket(db, tenant, assigned_to=agent.id)
    res_b = _request(db, b, agent)
    r = client.post(URL, json=_body(db, a, rating_request_token=res_b.token))
    assert r.status_code == 400 and r.json()["detail"]["code"] == "rating_request_invalid"


def test_reused_token_409_object(client, db, tenant, agent):
    t = make_ticket(db, tenant, assigned_to=agent.id)
    res = _request(db, t, agent)
    assert client.post(URL, json=_body(db, t, rating_request_token=res.token)).status_code == 201
    again = client.post(URL, json=_body(db, t, rating_request_token=res.token))
    assert again.status_code == 409 and again.json()["detail"]["code"] == "already_rated"


def test_expired_token_410(client, db, tenant, agent):
    t = make_ticket(db, tenant, assigned_to=agent.id)
    res = _request(db, t, agent)
    res.request.expires_at = utcnow_aware() - timedelta(seconds=1)
    db.commit()
    r = client.post(URL, json=_body(db, t, rating_request_token=res.token))
    assert r.status_code == 410 and r.json()["detail"]["code"] == "rating_request_expired"


def test_superseded_token_410_new_token_201(client, db, tenant, agent):
    t = make_ticket(db, tenant, assigned_to=agent.id)
    old = _request(db, t, agent)
    old.request.created_at = utcnow_aware() - timedelta(minutes=11)
    db.commit()
    new = _request(db, t, agent)
    stale = client.post(URL, json=_body(db, t, rating_request_token=old.token))
    assert stale.status_code == 410
    fresh = client.post(URL, json=_body(db, t, rating_request_token=new.token))
    assert fresh.status_code == 201


def test_legacy_resolution_rating_and_human_rating_coexist(client, db, tenant, agent):
    t = make_ticket(db, tenant, assigned_to=agent.id)
    res = _request(db, t, agent)
    old_widget = client.post(URL, json=_body(db, t, meta={"source": "agent_resolved"}))
    assert old_widget.status_code == 201 and old_widget.json()["rated_party"] == "ai"
    human = client.post(URL, json=_body(db, t, rating_request_token=res.token))
    assert human.status_code == 201 and human.json()["rated_party"] == "human"


def test_comment_over_500_is_422(client, db, tenant):
    t = make_ticket(db, tenant)
    assert client.post(URL, json=_body(db, t, comment="x" * 501)).status_code == 422
    assert client.post(URL, json=_body(db, t, comment="x" * 500)).status_code == 201


def test_rate_limited_429_object(client, db, tenant):
    t = make_ticket(db, tenant)
    with patch.object(
        rating_rate_limiter, "get_current_usage", AsyncMock(return_value={"calls": 20})
    ):
        r = client.post(URL, json=_body(db, t))
    assert r.status_code == 429
    assert r.json()["detail"]["code"] == "rating_rate_limited"
```

- [ ] **Step 2: Run to verify failure**

Run: `poetry run pytest tests/rating/test_public_rating_submit.py -v`
Expected: FAIL (`ImportError: rating_rate_limiter`).

- [ ] **Step 3: Implement**

`app/schemas/review.py` — replace lines 13-51 with:
```python
class SessionRatingBase(BaseModel):
    """Base session rating schema."""

    score: int = Field(..., ge=1, le=5, description="Rating score 1-5")
    comment: str | None = Field(None, max_length=2000, description="Optional feedback comment")
    meta: dict | None = Field(None, description="Additional context")


class SessionRatingCreate(SessionRatingBase):
    """Schema for creating a session rating (public endpoint).

    Unknown fields (e.g. a client-sent rated_agent_id) are ignored: the rated agent is
    derived server-side from rating_request_token only (N4-D2).
    """

    comment: str | None = Field(None, max_length=500, description="Optional comment (≤ 500)")
    session_id: UUID = Field(..., description="Conversation session ID")
    user_identifier: str = Field(
        ..., min_length=1, max_length=255, description="User identifier for verification"
    )
    rating_request_token: str | None = Field(
        None,
        min_length=1,
        max_length=128,
        description="Token from the rating_requested frame / message meta; rates that agent",
    )


class SessionRatingSubmitResponse(SessionRatingBase):
    """201 body of the public POST (no staff-only fields)."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    session_id: UUID
    user_identifier: str
    created_at: datetime
    updated_at: datetime
    rated_agent_id: UUID | None = None
    source: str = "idle_prompt"
    rating_request_id: UUID | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def rated_party(self) -> str:
        return "human" if self.rated_agent_id else "ai"


class SessionRatingResponse(SessionRatingSubmitResponse):
    """Staff list item: adds the agent's name and the ticket the request came from."""

    rated_agent_name: str | None = None
    ticket_id: UUID | None = None
    ticket_number: str | None = None


class SessionRatingListResponse(BaseModel):
    """Schema for paginated session ratings list."""

    ratings: list[SessionRatingResponse]
    total: int
    average_score: float | None = Field(None, description="Average rating score")
    csat_percent: float | None = Field(
        None, description="% of filtered ratings scoring >= 4 (1 dp); null when none"
    )
    page: int
    page_size: int
```
and add `computed_field` to the pydantic import line. Export `SessionRatingSubmitResponse` from `app/schemas/__init__.py` next to `SessionRatingResponse`.

`app/utils/http_rate_limiter.py` — after `video_register_rate_limiter`:
```python
# Public rating submit (N4): 20/hour/IP, own bucket so it never eats chat/upload quota.
rating_rate_limiter = RedisRateLimiter(key_prefix="http_rating", max_calls=20, window_seconds=3600)
```
and after `chat_video_register_rate_limit = …`:
```python
async def rating_rate_limit(request: Request) -> None:
    """Public POST /reviews/session limiter; 429 uses the N4 object error shape."""
    try:
        await check_rate_limit(request, rating_rate_limiter, "ip")
    except HTTPException as exc:
        if exc.status_code != status.HTTP_429_TOO_MANY_REQUESTS:
            raise
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "rating_rate_limited",
                "message": "Too many ratings from this network. Try again later",
                "retry_after_seconds": rating_rate_limiter.window_seconds,
            },
            headers=exc.headers,
        ) from exc
```

`app/services/review_service.py` — imports: add `from app.db.models.review import RatingRequest, RatingSource` (merge into the existing review import), `from app.schemas.review import SessionRatingSubmitResponse` (merge), `from app.services.rating_request_service import hash_token`, `from app.utils.datetime import ensure_utc_aware, utcnow_aware`. Add above `class ReviewService`:
```python
_RESOLUTION_META_SOURCES = ("agent_resolved", "ticket_closed")


def legacy_source(meta: dict | None) -> str:
    """Untokened ratings: same mapping as the n4agentrating backfill."""
    if (meta or {}).get("source") in _RESOLUTION_META_SOURCES:
        return RatingSource.RESOLUTION_PROMPT
    return RatingSource.IDLE_PROMPT


class RatingSubmitError(Exception):
    """Token-path refusal with the spec's object error body."""

    STATUS = {"rating_request_invalid": 400, "already_rated": 409, "rating_request_expired": 410}
    MESSAGES = {
        "rating_request_invalid": "This rating link is not valid for this conversation",
        "already_rated": "This conversation has already been rated for this agent",
        "rating_request_expired": "This rating request has expired",
    }

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code
        self.status_code = self.STATUS[code]

    def detail(self) -> dict:
        return {"code": self.code, "message": self.MESSAGES[self.code]}
```
Replace `create_session_rating` (lines 36-79) with:
```python
    def create_session_rating(
        self, rating_data: SessionRatingCreate
    ) -> SessionRatingSubmitResponse:
        """Create a rating. Untokened = AI/no-human party (legacy); tokened = that agent.

        Raises:
            ValueError: legacy string errors (session not found, identifier mismatch,
                duplicate AI rating) — byte-identical to pre-N4.
            RatingSubmitError: token-path refusals (object errors).
        """
        stmt = select(ConversationSession).where(ConversationSession.id == rating_data.session_id)
        session = self.db.execute(stmt).scalar_one_or_none()

        if not session:
            raise ValueError("Session not found")

        if session.user_identifier != rating_data.user_identifier:
            raise ValueError("User identifier does not match session")

        if rating_data.rating_request_token:
            return self._create_tokened_rating(session, rating_data)

        ai_exists = self.db.execute(
            select(SessionRating.id).where(
                SessionRating.session_id == session.id, SessionRating.rated_agent_id.is_(None)
            )
        ).first()
        if ai_exists:
            raise ValueError("Rating already exists for this session")

        db_rating = SessionRating(
            tenant_id=session.tenant_id,
            session_id=rating_data.session_id,
            score=rating_data.score,
            comment=rating_data.comment,
            user_identifier=rating_data.user_identifier,
            meta=rating_data.meta or {},
            rated_agent_id=None,
            source=legacy_source(rating_data.meta),
        )
        try:
            self.db.add(db_rating)
            self.db.commit()
            self.db.refresh(db_rating)
        except IntegrityError as err:
            self.db.rollback()
            raise ValueError("Rating already exists for this session") from err

        return SessionRatingSubmitResponse.model_validate(db_rating)

    def _create_tokened_rating(
        self, session: ConversationSession, rating_data: SessionRatingCreate
    ) -> SessionRatingSubmitResponse:
        """Spec §4.3.3 token path. The agent comes from the request row, never the client."""
        now = utcnow_aware()
        request = self.db.execute(
            select(RatingRequest)
            .where(RatingRequest.token_hash == hash_token(rating_data.rating_request_token))
            .with_for_update()
        ).scalar_one_or_none()
        if request is None or request.session_id != session.id:
            raise RatingSubmitError("rating_request_invalid")
        already = self.db.execute(
            select(SessionRating.id).where(
                SessionRating.session_id == session.id,
                SessionRating.rated_agent_id == request.agent_id,
            )
        ).first()
        if request.fulfilled_at is not None or already:
            raise RatingSubmitError("already_rated")
        if ensure_utc_aware(request.expires_at) <= now:
            raise RatingSubmitError("rating_request_expired")

        db_rating = SessionRating(
            tenant_id=session.tenant_id,
            session_id=session.id,
            score=rating_data.score,
            comment=rating_data.comment,
            user_identifier=rating_data.user_identifier,
            meta=rating_data.meta or {},
            rated_agent_id=request.agent_id,
            source=request.source,
            rating_request_id=request.id,
        )
        request.fulfilled_at = now
        try:
            self.db.add(db_rating)
            self.db.commit()
            self.db.refresh(db_rating)
        except IntegrityError as err:
            self.db.rollback()
            raise RatingSubmitError("already_rated") from err
        return SessionRatingSubmitResponse.model_validate(db_rating)
```

`app/api/v1/endpoints/reviews.py` — imports: add `from app.schemas.review import SessionRatingSubmitResponse` (merge), `from app.services.review_service import RatingSubmitError, ReviewService` (merge), `from app.utils.http_rate_limiter import rating_rate_limit`. Change the POST decorator to `response_model=SessionRatingSubmitResponse` and `dependencies=[Depends(rating_rate_limit)]`; in the handler add, as the **first** `except` clause:
```python
    except RatingSubmitError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail()) from e
```
and change the success log to ids only:
```python
        logger.info(
            f"Session rating created: session_id={rating_data.session_id}, "
            f"score={rating_data.score}, tokened={bool(rating_data.rating_request_token)}"
        )
```
Update the docstring's Returns list: `400 rating_request_invalid`, `409 already_rated`, `410 rating_request_expired`, `429 rating_rate_limited`.

- [ ] **Step 4: Run tests**

Run: `poetry run pytest tests/rating/test_public_rating_submit.py tests/rating -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/schemas/review.py app/schemas/__init__.py app/services/review_service.py app/api/v1/endpoints/reviews.py app/utils/http_rate_limiter.py tests/rating/test_public_rating_submit.py
git commit -m "feat(ratings): token-gated public rating submit, 500-char comments, 20/h IP limit"
```

---

### Task 7: `GET /reviews/session` filters, support-agent scoping, enrichment; platform item fields

**Review:** Opus (permission scoping: support agents must never see another agent's ratings).

**Files:**
- Modify: `app/services/rating_read_service.py` (append `csat_percent_expr`, `enrich_ratings`)
- Modify: `app/services/review_service.py:99-182` (`list_session_ratings`), `:439-516` (`get_all_session_ratings` items)
- Modify: `app/api/v1/endpoints/reviews.py:98-165` (`list_session_ratings`)
- Test: `tests/rating/test_ratings_list_scope.py`

**Interfaces:**
- Consumes: Task 3 `agent_names`, `ticket_refs`; Task 6 `SessionRatingResponse`, `SessionRatingListResponse.csat_percent`.
- Produces: `csat_percent_expr()` (SQL `AVG(CASE WHEN score >= 4 THEN 100.0 ELSE 0.0 END)`), `enrich_ratings(db, ratings) -> list[SessionRatingResponse]`. `ReviewService.list_session_ratings(..., rated_agent_id=None, rated_party=None, source=None)`. Query params `rated_agent_id`, `rated_party` (`ai`|`human`), `source`.

- [ ] **Step 1: Write the failing tests**

`tests/rating/test_ratings_list_scope.py`:
```python
"""GET /reviews/session: filters, CSAT, enrichment, support-agent own-only scope (§4.3.4)."""

from app.db.models.review import RatingSource
from app.services.rating_request_service import RatingRequestService
from app.services.review_service import ReviewService
from tests.rating.conftest import headers, make_rating, make_ticket

URL = "/api/v1/reviews/session"


def _seed(db, tenant, agent, other_agent):
    """AI 3, agent 5 (tokened, has ticket), agent 4, other_agent 2."""
    t1 = make_ticket(db, tenant, assigned_to=agent.id)
    req = RatingRequestService(db).create_request(
        t1.id, source=RatingSource.MANUAL_REQUEST, requested_by=agent
    ).request
    make_rating(db, session_id=t1.session_id, tenant_id=tenant.id, score=3)
    make_rating(
        db,
        session_id=t1.session_id,
        tenant_id=tenant.id,
        score=5,
        rated_agent_id=agent.id,
        source=RatingSource.MANUAL_REQUEST,
        rating_request_id=req.id,
    )
    t2 = make_ticket(db, tenant, assigned_to=agent.id)
    make_rating(
        db,
        session_id=t2.session_id,
        tenant_id=tenant.id,
        score=4,
        rated_agent_id=agent.id,
        source=RatingSource.AUTO_RESOLVE,
    )
    t3 = make_ticket(db, tenant, assigned_to=other_agent.id)
    make_rating(
        db,
        session_id=t3.session_id,
        tenant_id=tenant.id,
        score=2,
        rated_agent_id=other_agent.id,
        source=RatingSource.AUTO_RESOLVE,
    )
    return t1


def _get(client, user, **params):
    return client.get(URL, headers=headers(user), params=params)


def test_admin_sees_all_with_enrichment_and_csat(client, db, tenant, agent, other_agent, admin):
    t1 = _seed(db, tenant, agent, other_agent)
    body = _get(client, admin).json()
    assert body["total"] == 4
    assert body["csat_percent"] == 50.0  # 5 and 4 of [3, 5, 4, 2]
    by_score = {r["score"]: r for r in body["ratings"]}
    assert by_score[3]["rated_party"] == "ai" and by_score[3]["rated_agent_name"] is None
    assert by_score[5]["rated_agent_name"] == "Sam Rivers"
    assert by_score[5]["ticket_number"] == t1.ticket_number
    assert by_score[5]["ticket_id"] == str(t1.id)
    assert by_score[4]["ticket_number"] is None  # no request -> no ticket ref


def test_admin_filters(client, db, tenant, agent, other_agent, admin):
    _seed(db, tenant, agent, other_agent)
    assert _get(client, admin, rated_party="ai").json()["total"] == 1
    assert _get(client, admin, rated_party="human").json()["total"] == 3
    assert _get(client, admin, rated_agent_id=str(other_agent.id)).json()["total"] == 1
    assert _get(client, admin, source="auto_resolve").json()["total"] == 2


def test_support_agent_is_scoped_to_own(client, db, tenant, agent, other_agent):
    _seed(db, tenant, agent, other_agent)
    body = _get(client, agent).json()
    assert body["total"] == 2
    assert {r["rated_agent_id"] for r in body["ratings"]} == {str(agent.id)}
    assert body["csat_percent"] == 100.0
    assert _get(client, agent, rated_agent_id=str(agent.id)).json()["total"] == 2


def test_support_agent_other_agent_id_403(client, db, tenant, agent, other_agent):
    _seed(db, tenant, agent, other_agent)
    r = _get(client, agent, rated_agent_id=str(other_agent.id))
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "rating_scope_forbidden"


def test_support_agent_ai_filter_is_empty(client, db, tenant, agent, other_agent):
    _seed(db, tenant, agent, other_agent)
    body = _get(client, agent, rated_party="ai").json()
    assert (body["total"], body["ratings"], body["csat_percent"]) == (0, [], None)


def test_viewer_sees_nothing_of_others(client, db, tenant, agent, other_agent, viewer):
    _seed(db, tenant, agent, other_agent)
    assert _get(client, viewer).json()["total"] == 0


def test_csat_null_when_no_ratings(client, db, tenant, admin):
    body = _get(client, admin).json()
    assert (body["total"], body["average_score"], body["csat_percent"]) == (0, None, None)


def test_legacy_long_comment_still_lists(client, db, tenant, admin):
    t = make_ticket(db, tenant)
    make_rating(db, session_id=t.session_id, tenant_id=tenant.id, comment="y" * 1500)
    r = _get(client, admin)
    assert r.status_code == 200 and r.json()["ratings"][0]["comment"] == "y" * 1500


def test_platform_all_ratings_items_carry_new_fields(db, tenant, agent, other_agent):
    _seed(db, tenant, agent, other_agent)
    out = ReviewService(db).get_all_session_ratings(tenant_id=tenant.id)
    item = next(r for r in out["ratings"] if r.score == 5)
    assert item.rated_party == "human" and item.rated_agent_name == "Sam Rivers"
    assert item.ticket_number is not None
```

- [ ] **Step 2: Run to verify failure**

Run: `poetry run pytest tests/rating/test_ratings_list_scope.py -v`
Expected: FAIL (support agent sees all; no `csat_percent`).

- [ ] **Step 3: Implement**

Append to `app/services/rating_read_service.py` (add `from sqlalchemy import case, func` and `from app.schemas.review import SessionRatingResponse` to its imports):
```python
def csat_percent_expr():
    """AVG of 100/0 per rating = % of ratings scoring >= 4 (N4-D6)."""
    return func.avg(case((SessionRating.score >= 4, 100.0), else_=0.0))


def enrich_ratings(db: Session, ratings: list[SessionRating]) -> list[SessionRatingResponse]:
    names = agent_names(db, (r.rated_agent_id for r in ratings))
    refs = ticket_refs(db, (r.rating_request_id for r in ratings))
    items = []
    for rating in ratings:
        item = SessionRatingResponse.model_validate(rating)
        item.rated_agent_name = names.get(rating.rated_agent_id) if rating.rated_agent_id else None
        item.ticket_id, item.ticket_number = refs.get(rating.rating_request_id, (None, None))
        items.append(item)
    return items
```

`app/services/review_service.py` — add `true` to the sqlalchemy import and `from app.services.rating_read_service import csat_percent_expr, enrich_ratings`. Replace `list_session_ratings` (lines 99-182) with:
```python
    def list_session_ratings(
        self,
        tenant_id: UUID | None = None,
        session_id: UUID | None = None,
        min_score: int | None = None,
        max_score: int | None = None,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
        rated_agent_id: UUID | None = None,
        rated_party: str | None = None,
        source: str | None = None,
    ) -> SessionRatingListResponse:
        """List ratings with filters. Scoping (who may pass which filter) is the endpoint's job."""
        filters = []
        if tenant_id is not None:
            filters.append(SessionRating.tenant_id == tenant_id)
        if session_id:
            filters.append(SessionRating.session_id == session_id)
        if min_score is not None:
            filters.append(SessionRating.score >= min_score)
        if max_score is not None:
            filters.append(SessionRating.score <= max_score)
        if start_date:
            filters.append(SessionRating.created_at >= start_date)
        if end_date:
            filters.append(SessionRating.created_at <= end_date)
        if rated_agent_id is not None:
            filters.append(SessionRating.rated_agent_id == rated_agent_id)
        if rated_party == "ai":
            filters.append(SessionRating.rated_agent_id.is_(None))
        elif rated_party == "human":
            filters.append(SessionRating.rated_agent_id.isnot(None))
        if source:
            filters.append(SessionRating.source == source)
        where = and_(*filters) if filters else true()

        total = self.db.execute(select(func.count(SessionRating.id)).where(where)).scalar() or 0
        average_score, csat = self.db.execute(
            select(func.avg(SessionRating.score), csat_percent_expr()).where(where)
        ).one()
        ratings = (
            self.db.execute(
                select(SessionRating)
                .where(where)
                .order_by(desc(SessionRating.created_at))
                .limit(limit)
                .offset(offset)
            )
            .scalars()
            .all()
        )
        return SessionRatingListResponse(
            ratings=enrich_ratings(self.db, list(ratings)),
            total=total,
            average_score=float(average_score) if average_score else None,
            csat_percent=round(float(csat), 1) if total and csat is not None else None,
            page=(offset // limit) + 1 if limit > 0 else 1,
            page_size=limit,
        )
```
In `get_all_session_ratings` replace `"ratings": [SessionRatingResponse.model_validate(r) for r in ratings],` with `"ratings": enrich_ratings(self.db, list(ratings)),`.

`app/api/v1/endpoints/reviews.py` — imports: `from typing import Literal`, `from app.db.models.user import UserRole`, `SessionRatingListResponse` already imported. New params on `list_session_ratings` (before `limit`):
```python
    rated_agent_id: UUID | None = Query(
        None, description="Filter by rated agent (support agents: own id only)"
    ),
    rated_party: Literal["ai", "human"] | None = Query(None, description="AI vs human ratings"),
    source: Literal["idle_prompt", "resolution_prompt", "manual_request", "auto_resolve"]
    | None = Query(None, description="Rating source"),
```
Replace the handler body (lines ~134-165) with (scoping happens **before** the broad `try`, so the 403 is not turned into a 500):
```python
    tenant_id = None
    if auth.auth_type == "tenant_user":
        tenant_id = auth.tenant_id
        user = auth.user
        # N4-D5: anyone below tenant admin sees only ratings of themselves.
        if user.role != UserRole.TENANT_ADMIN:
            if rated_agent_id is not None and rated_agent_id != user.id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail={
                        "code": "rating_scope_forbidden",
                        "message": "You can only view your own ratings",
                    },
                )
            if rated_party == "ai":
                return SessionRatingListResponse(
                    ratings=[],
                    total=0,
                    average_score=None,
                    csat_percent=None,
                    page=(offset // limit) + 1,
                    page_size=limit,
                )
            rated_agent_id = user.id
        logger.info(f"Tenant user viewing ratings for tenant {tenant_id}")
    else:
        logger.info("Platform admin viewing all ratings (system-wide)")

    try:
        return ReviewService(db).list_session_ratings(
            tenant_id=tenant_id,
            session_id=session_id,
            min_score=min_score,
            max_score=max_score,
            start_date=start_date,
            end_date=end_date,
            limit=limit,
            offset=offset,
            rated_agent_id=rated_agent_id,
            rated_party=rated_party,
            source=source,
        )
    except Exception as e:
        logger.error(f"Error listing session ratings: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve session ratings",
        ) from e
```
Update the docstring's Access Control: "Tenant admins: all tenant ratings. Support agents and viewers: only ratings of themselves (`rated_agent_id` forced to self; another id → 403 `rating_scope_forbidden`; `rated_party=ai` → empty). Platform admins: all tenants."

- [ ] **Step 4: Run tests**

Run: `poetry run pytest tests/rating -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/rating_read_service.py app/services/review_service.py app/api/v1/endpoints/reviews.py tests/rating/test_ratings_list_scope.py
git commit -m "feat(ratings): ratings list filters, CSAT, enrichment; support agents scoped to own ratings"
```

---

### Task 8: Per-agent CSAT in analytics + `GET /agent/me/ratings`

**Review:** Opus (own-only scoping of `/agent/me/ratings`).

**Files:**
- Modify: `app/services/rating_read_service.py` (append `RatingStats`, `per_agent_rating_stats`, `agent_rating_stats`)
- Modify: `app/schemas/analytics.py:81-110`
- Modify: `app/services/analytics_service.py:1-31` (imports), `~:308-322` (`get_agent_performance` return), `:324-389` (`get_all_agents_performance`)
- Create: `app/api/v1/endpoints/agent_ratings.py`
- Modify: `app/api/v1/api.py` (import `agent_ratings`; mount under `/agent` after `agent_app` at :279)
- Test: `tests/rating/test_rating_stats.py`, `tests/rating/test_my_ratings.py`

**Interfaces:**
- Consumes: Task 7 `csat_percent_expr`; Task 3 `ticket_refs`, `MyRatingItem`, `MyRatingsResponse`.
- Produces: `@dataclass(frozen=True) RatingStats(count: int = 0, average: float | None = None, csat_percent: float | None = None)`, `NO_RATINGS`, `per_agent_rating_stats(db, tenant_id, start, end) -> dict[UUID, RatingStats]` (one grouped query), `agent_rating_stats(db, tenant_id, agent_id, start, end) -> RatingStats`. Window = `created_at >= start AND created_at < end`. `AgentSummaryResponse` / `AgentPerformanceResponse` gain `ratings_count: int = 0`, `average_rating: float | None`, `csat_percent: float | None`. Route `GET /api/v1/agent/me/ratings`.

- [ ] **Step 1: Write the failing tests**

`tests/rating/test_rating_stats.py`:
```python
"""CSAT math (N4-D6) and analytics per-agent fields (§4.3.6)."""

from datetime import timedelta

from app.db.models.review import RatingSource
from app.services.rating_read_service import (
    NO_RATINGS,
    agent_rating_stats,
    per_agent_rating_stats,
)
from app.utils.datetime import utcnow_aware, utcnow_naive
from tests.rating.conftest import headers, make_rating, make_ticket


def _rate(db, tenant, agent_id, score, *, days_ago=0):
    t = make_ticket(db, tenant, assigned_to=agent_id)
    return make_rating(
        db,
        session_id=t.session_id,
        tenant_id=tenant.id,
        score=score,
        rated_agent_id=agent_id,
        source=RatingSource.AUTO_RESOLVE,
        created_at=utcnow_naive() - timedelta(days=days_ago, minutes=1),
    )


def _window():
    end = utcnow_aware()
    return end - timedelta(days=30), end


def test_grouped_stats_math_window_and_ai_excluded(db, tenant, agent, other_agent):
    for score in (5, 4, 2):
        _rate(db, tenant, agent.id, score)
    _rate(db, tenant, agent.id, 1, days_ago=40)  # outside the window
    t = make_ticket(db, tenant)
    make_rating(db, session_id=t.session_id, tenant_id=tenant.id, score=1)  # AI
    stats = per_agent_rating_stats(db, tenant.id, *_window())
    assert stats[agent.id].count == 3
    assert stats[agent.id].average == 3.67
    assert stats[agent.id].csat_percent == 66.7
    assert other_agent.id not in stats
    assert agent_rating_stats(db, tenant.id, other_agent.id, *_window()) == NO_RATINGS


def test_analytics_agents_rows_carry_rating_fields(client, db, tenant, agent, other_agent, admin):
    _rate(db, tenant, agent.id, 5)
    _rate(db, tenant, agent.id, 3)
    rows = {r["agent_id"]: r for r in client.get(
        "/api/v1/analytics/agents", headers=headers(admin)
    ).json()}
    assert (rows[str(agent.id)]["ratings_count"], rows[str(agent.id)]["average_rating"]) == (2, 4.0)
    assert rows[str(agent.id)]["csat_percent"] == 50.0
    empty = rows[str(other_agent.id)]
    assert (empty["ratings_count"], empty["average_rating"], empty["csat_percent"]) == (
        0,
        None,
        None,
    )


def test_analytics_agent_detail_for_self(client, db, tenant, agent):
    _rate(db, tenant, agent.id, 4)
    body = client.get(f"/api/v1/analytics/agents/{agent.id}", headers=headers(agent)).json()
    assert (body["ratings_count"], body["average_rating"], body["csat_percent"]) == (1, 4.0, 100.0)
```

`tests/rating/test_my_ratings.py`:
```python
"""GET /agent/me/ratings: own only, no customer identity, newest 20 (§4.3.5)."""

from datetime import timedelta

from app.db.models.review import RatingSource
from app.services.rating_request_service import RatingRequestService
from app.utils.datetime import utcnow_naive
from tests.rating.conftest import headers, make_rating, make_ticket

URL = "/api/v1/agent/me/ratings"


def test_returns_only_own_with_stats_and_ticket_number(client, db, tenant, agent, other_agent):
    t = make_ticket(db, tenant, assigned_to=agent.id)
    req = RatingRequestService(db).create_request(
        t.id, source=RatingSource.MANUAL_REQUEST, requested_by=agent
    ).request
    make_rating(
        db,
        session_id=t.session_id,
        tenant_id=tenant.id,
        score=5,
        comment="kind",
        rated_agent_id=agent.id,
        source=RatingSource.MANUAL_REQUEST,
        rating_request_id=req.id,
    )
    t2 = make_ticket(db, tenant, assigned_to=other_agent.id)
    make_rating(db, session_id=t2.session_id, tenant_id=tenant.id, score=1, rated_agent_id=other_agent.id)
    body = client.get(URL, headers=headers(agent)).json()
    assert (body["count"], body["average_score"], body["csat_percent"]) == (1, 5.0, 100.0)
    assert len(body["recent"]) == 1
    item = body["recent"][0]
    assert set(item) == {"id", "score", "comment", "created_at", "ticket_number", "source"}
    assert (item["ticket_number"], item["comment"]) == (t.ticket_number, "kind")
    assert set(body) == {
        "period_start",
        "period_end",
        "count",
        "average_score",
        "csat_percent",
        "recent",
    }


def test_recent_is_newest_20_and_stats_cover_all(client, db, tenant, agent):
    base = utcnow_naive() - timedelta(days=1)
    for i in range(21):
        t = make_ticket(db, tenant, assigned_to=agent.id)
        make_rating(
            db,
            session_id=t.session_id,
            tenant_id=tenant.id,
            score=4,
            rated_agent_id=agent.id,
            source=RatingSource.AUTO_RESOLVE,
            created_at=base + timedelta(minutes=i),
        )
    body = client.get(URL, headers=headers(agent)).json()
    assert body["count"] == 21 and len(body["recent"]) == 20
    stamps = [r["created_at"] for r in body["recent"]]
    assert stamps == sorted(stamps, reverse=True)


def test_empty_state_is_nulls(client, db, tenant, agent):
    body = client.get(URL, headers=headers(agent)).json()
    assert (body["count"], body["average_score"], body["csat_percent"], body["recent"]) == (
        0,
        None,
        None,
        [],
    )


def test_viewer_forbidden(client, db, tenant, viewer):
    assert client.get(URL, headers=headers(viewer)).status_code == 403


def test_end_before_start_400(client, db, tenant, agent):
    r = client.get(
        URL,
        headers=headers(agent),
        params={"start_date": "2026-10-10T00:00:00Z", "end_date": "2026-10-01T00:00:00Z"},
    )
    assert r.status_code == 400 and r.json()["detail"] == "End date must be after start date"
```

- [ ] **Step 2: Run to verify failure**

Run: `poetry run pytest tests/rating/test_rating_stats.py tests/rating/test_my_ratings.py -v`
Expected: FAIL (`ImportError: NO_RATINGS`; 404 on `/agent/me/ratings`).

- [ ] **Step 3: Implement**

Append to `app/services/rating_read_service.py` (add `from dataclasses import dataclass`, `from datetime import datetime`, `from app.utils.datetime import ensure_utc_aware`):
```python
@dataclass(frozen=True)
class RatingStats:
    count: int = 0
    average: float | None = None
    csat_percent: float | None = None


NO_RATINGS = RatingStats()


def _to_stats(count, average, csat) -> RatingStats:
    if not count:
        return NO_RATINGS
    return RatingStats(int(count), round(float(average), 2), round(float(csat), 1))


def _in_window(stmt, start: datetime, end: datetime):
    """Window on the rating's own created_at (N4-D6): [start, end)."""
    return stmt.where(
        SessionRating.created_at >= ensure_utc_aware(start),
        SessionRating.created_at < ensure_utc_aware(end),
    )


def per_agent_rating_stats(
    db: Session, tenant_id: UUID, start: datetime, end: datetime
) -> dict[UUID, RatingStats]:
    """One grouped query for every rated agent in the tenant (no N+1)."""
    stmt = (
        select(
            SessionRating.rated_agent_id,
            func.count(SessionRating.id),
            func.avg(SessionRating.score),
            csat_percent_expr(),
        )
        .where(SessionRating.tenant_id == tenant_id, SessionRating.rated_agent_id.isnot(None))
        .group_by(SessionRating.rated_agent_id)
    )
    rows = db.execute(_in_window(stmt, start, end)).all()
    return {agent_id: _to_stats(c, a, s) for agent_id, c, a, s in rows}


def agent_rating_stats(
    db: Session, tenant_id: UUID, agent_id: UUID, start: datetime, end: datetime
) -> RatingStats:
    stmt = select(
        func.count(SessionRating.id), func.avg(SessionRating.score), csat_percent_expr()
    ).where(SessionRating.tenant_id == tenant_id, SessionRating.rated_agent_id == agent_id)
    count, average, csat = db.execute(_in_window(stmt, start, end)).one()
    return _to_stats(count, average, csat)
```

`app/schemas/analytics.py` — append to both `AgentPerformanceResponse` (after `avg_response_time_minutes`) and `AgentSummaryResponse` (after `total_tickets_resolved`):
```python
    ratings_count: int = Field(0, description="Ratings received in the window (rating created_at)")
    average_rating: float | None = Field(None, description="Average score in the window (2 dp)")
    csat_percent: float | None = Field(
        None, description="% of window ratings scoring >= 4 (1 dp); null when none"
    )
```
(`Field` is already imported there; add it if not.)

`app/services/analytics_service.py` — import `from app.services.rating_read_service import NO_RATINGS, agent_rating_stats, per_agent_rating_stats`. In `get_agent_performance`, before `return AgentPerformanceResponse(`:
```python
        rating = agent_rating_stats(self.db, tenant_id, agent_id, start_date, end_date)
```
and add to the constructor:
```python
            ratings_count=rating.count,
            average_rating=rating.average,
            csat_percent=rating.csat_percent,
```
In `get_all_agents_performance`, right after the default-range line add:
```python
        ratings = per_agent_rating_stats(self.db, tenant_id, start_date, end_date)
```
and in the `AgentSummaryResponse(...)` call (lines ~379-386) add:
```python
                    ratings_count=ratings.get(agent.id, NO_RATINGS).count,
                    average_rating=ratings.get(agent.id, NO_RATINGS).average,
                    csat_percent=ratings.get(agent.id, NO_RATINGS).csat_percent,
```

`app/api/v1/endpoints/agent_ratings.py`:
```python
"""Agent app: own ratings — GET /agent/me/ratings (spec §4.3.5, N4-D5).

Own ratings only, by construction (filter is the caller's id). No customer identity
(no user_identifier, visitor name or session id). Not gated by the analytics plan
capability: an agent's own feedback is not a plan analytics feature.
"""

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.api.deps import get_current_agent
from app.db.models.review import SessionRating
from app.db.session import get_db
from app.schemas.rating import MyRatingItem, MyRatingsResponse
from app.schemas.user import UserResponse
from app.services.rating_read_service import agent_rating_stats, ticket_refs
from app.utils.datetime import ensure_utc_aware, utcnow_aware

router = APIRouter()

RECENT_LIMIT = 20
DEFAULT_WINDOW = timedelta(days=30)


def _window(start: datetime | None, end: datetime | None) -> tuple[datetime, datetime]:
    end_at = ensure_utc_aware(end) if end else utcnow_aware()
    start_at = ensure_utc_aware(start) if start else end_at - DEFAULT_WINDOW
    return start_at, end_at


@router.get("/me/ratings", response_model=MyRatingsResponse)
def get_my_ratings(
    start_date: datetime | None = Query(None, description="Window start (default end - 30 d)"),
    end_date: datetime | None = Query(None, description="Window end (default now)"),
    db: Session = Depends(get_db),
    current_agent: UserResponse = Depends(get_current_agent),
):
    start, end = _window(start_date, end_date)
    if end < start:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="End date must be after start date",
        )
    stats = agent_rating_stats(db, current_agent.tenant_id, current_agent.id, start, end)
    rows = (
        db.execute(
            select(SessionRating)
            .where(
                SessionRating.tenant_id == current_agent.tenant_id,
                SessionRating.rated_agent_id == current_agent.id,
                SessionRating.created_at >= start,
                SessionRating.created_at < end,
            )
            .order_by(desc(SessionRating.created_at))
            .limit(RECENT_LIMIT)
        )
        .scalars()
        .all()
    )
    refs = ticket_refs(db, (r.rating_request_id for r in rows))
    return MyRatingsResponse(
        period_start=start,
        period_end=end,
        count=stats.count,
        average_score=stats.average,
        csat_percent=stats.csat_percent,
        recent=[
            MyRatingItem(
                id=r.id,
                score=r.score,
                comment=r.comment,
                created_at=r.created_at,
                ticket_number=refs.get(r.rating_request_id, (None, None))[1],
                source=r.source,
            )
            for r in rows
        ],
    )
```

`app/api/v1/api.py`: add `agent_ratings,` to the import list (after `agent_app,`), and directly under the `agent_app` mount:
```python
api_router.include_router(agent_ratings.router, prefix="/agent", tags=["agent-app"])
```

- [ ] **Step 4: Run tests**

Run: `poetry run pytest tests/rating -v && poetry run pytest tests -k "analytics" -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/rating_read_service.py app/schemas/analytics.py app/services/analytics_service.py app/api/v1/endpoints/agent_ratings.py app/api/v1/api.py tests/rating/test_rating_stats.py tests/rating/test_my_ratings.py
git commit -m "feat(ratings): per-agent CSAT in analytics; GET /agent/me/ratings (own only)"
```

---

### Task 9: Purge semantics (N4-D10)

**Files:**
- Modify: `app/services/account_purge_service.py:36-45` (FK count text), `:95-117` (de-identified list), `:160-175` (tenant list), `:239` (import), `:301` (tenant delete), `:447-540` (`_deidentify_user_references`)
- Modify: `tests/services/test_account_purge_service.py` (imports; new test; seed in `_seed_every_tenant_scoped_table` after the `session_ratings` `_add`, ~:921)

**Interfaces:**
- Consumes: Task 1 `RatingRequest`, `SessionRating` columns.
- Produces: `purge_user` NULLs `rating_requests.requested_by`, keeps `session_ratings.rated_agent_id` and `rating_requests.agent_id`; `purge_tenant` deletes `rating_requests` right after `session_ratings`.

- [ ] **Step 1: Write the failing tests**

In `tests/services/test_account_purge_service.py`: add `from datetime import timedelta` to the imports and change the review import to `from app.db.models.review import PlatformFeedback, RatingRequest, SessionRating`. Add after `test_purge_user_deidentifies_other_tenant_owned_fk_references`:
```python
def test_purge_user_keeps_rating_attribution_and_nulls_requested_by(db_session):
    """N4-D10: NULLing rated_agent_id would turn a human rating into an AI one (and could
    collide with the AI partial unique index); the purged user row is a PII-free shell,
    so the attribution FKs are kept. requested_by is a "who did it" column -> NULL."""
    tenant = _make_tenant(db_session)
    agent = _make_user(db_session, tenant)
    admin = _make_user(db_session, tenant, role=UserRole.TENANT_ADMIN)
    session = _make_session(db_session, tenant, agent=agent)
    request = RatingRequest(
        tenant_id=tenant.id,
        session_id=session.id,
        agent_id=agent.id,
        requested_by=admin.id,
        source="manual_request",
        token_hash=uuid4().hex + uuid4().hex,
        expires_at=utcnow_aware() + timedelta(days=7),
    )
    db_session.add(request)
    db_session.flush()
    rating = SessionRating(
        tenant_id=tenant.id,
        session_id=session.id,
        score=4,
        user_identifier="u1",
        rated_agent_id=agent.id,
        source="manual_request",
        rating_request_id=request.id,
    )
    db_session.add(rating)
    db_session.flush()

    service = AccountPurgeService(db_session)
    service.purge_user(agent.id)
    service.purge_user(admin.id)

    db_session.refresh(rating)
    db_session.refresh(request)
    assert rating.rated_agent_id == agent.id
    assert request.agent_id == agent.id
    assert request.requested_by is None
```
In `_seed_every_tenant_scoped_table`, directly after the `session_ratings` `_add(...)` block:
```python
    _add(
        RatingRequest(
            tenant_id=tenant.id,
            session_id=(
                db_session.execute(
                    select(ConversationSession.id).where(ConversationSession.tenant_id == tenant.id)
                )
                .scalars()
                .first()
            ),
            agent_id=agent.id,
            source="manual_request",
            token_hash=uuid4().hex + uuid4().hex,
            expires_at=utcnow_aware() + timedelta(days=7),
        ),
        "rating_requests",
    )
```

- [ ] **Step 2: Run to verify failure** (Postgres needed)

```bash
docker start sr-ci-pg sr-ci-redis 2>/dev/null || true
DATABASE_URL=postgresql://test_user:test_password@localhost:55432/test_db \
  poetry run pytest tests/services/test_account_purge_service.py -k "rating or completeness" -v
```
Expected: FAIL — `requested_by` still set, and the completeness guard reports `rating_requests still has rows for the purged tenant`. (If `sr-ci-pg` does not exist yet, create it exactly as in Task 10 Step 2.)

- [ ] **Step 3: Implement**

`app/services/account_purge_service.py`:
- `:239` → `from app.db.models.review import PlatformFeedback, RatingRequest, SessionRating`.
- `purge_tenant`, right after `self.db.execute(delete(SessionRating).where(SessionRating.tenant_id == tenant_id))` (:301):
```python
        # session_ratings.rating_request_id -> rating_requests is SET NULL; ratings go first.
        self.db.execute(delete(RatingRequest).where(RatingRequest.tenant_id == tenant_id))
```
- `_deidentify_user_references`, after the `ProcessingJob` update:
```python
        # N4-D10: rating_requests.requested_by is a "who did it" column -> NULL.
        # session_ratings.rated_agent_id and rating_requests.agent_id are deliberately
        # KEPT: NULL would re-label a human rating as an AI rating; the user row survives
        # as a PII-free shell, so nothing personal remains through these FKs.
        self.db.execute(
            RatingRequest.__table__.update()
            .where(RatingRequest.requested_by == user_id)
            .values(requested_by=None)
        )
```
- Module docstring: in the "Every inbound FK to users.id" paragraph change "15 occurrences, all handled below" to "18 occurrences — 15 + N4's three on `session_ratings`/`rating_requests` — all handled below". In the de-identified list add `rating_requests.requested_by                     -> NULL`. Add, right after that list:
```
Kept on purpose (N4-D10): session_ratings.rated_agent_id and
rating_requests.agent_id. NULLing them would turn a human rating into an "AI"
rating and could collide with uq_session_ratings_session_ai; the purged user row
is a PII-free shell, so these FKs expose nothing personal.
```
In the `purge_tenant` table list add `rating_requests` after `session_ratings`.

- [ ] **Step 4: Run tests**

```bash
DATABASE_URL=postgresql://test_user:test_password@localhost:55432/test_db \
  poetry run pytest tests/services/test_account_purge_service.py -v
```
Expected: PASS (all, including the completeness guard).

- [ ] **Step 5: Commit**

```bash
git add app/services/account_purge_service.py tests/services/test_account_purge_service.py
git commit -m "feat(ratings): purge keeps rating attribution, nulls requested_by, deletes rating_requests per tenant"
```

---

### Task 10: Reproduce CI locally, reviews, PR

**Files:** none new (fixes only).

- [ ] **Step 1: Install the locked toolchain and check versions**

```bash
poetry install --no-interaction --no-root --with dev
poetry run ruff --version    # expect: ruff 0.8.6
poetry run black --version   # expect: black, 24.10.0 (…)
poetry run python -c "import websockets, httpx; print('deps ok')"
```
If a version differs, stop and fix the environment (do not use a global tool).

- [ ] **Step 2: Lint + format exactly as the `lint` job**

```bash
poetry run ruff check app/
poetry run black --check app/
```
Expected: both clean. Fix and re-run until they are.

- [ ] **Step 3: Full `test` job against throwaway Postgres 15 + Redis 7**

```bash
docker start sr-ci-pg 2>/dev/null || docker run -d --rm --name sr-ci-pg \
  -e POSTGRES_USER=test_user -e POSTGRES_PASSWORD=test_password -e POSTGRES_DB=test_db \
  -p 55432:5432 postgres:15-alpine
docker start sr-ci-redis 2>/dev/null || docker run -d --rm --name sr-ci-redis -p 56379:6379 redis:7-alpine
docker exec sr-ci-redis redis-cli FLUSHDB
```
Run the `test` job's pytest with the job's `env:` loaded from the workflow, ports rewritten, **values never echoed** (system `python3` has PyYAML; the poetry env does not):
```bash
python3 - <<'PY'
import os, yaml
wf = yaml.safe_load(open(".github/workflows/ci.yml"))
step = next(s for s in wf["jobs"]["test"]["steps"] if s.get("name") == "Run tests")
env = {k: str(v) for k, v in step["env"].items()}
env["DATABASE_URL"] = env["DATABASE_URL"].replace("localhost:5432", "localhost:55432")
env["REDIS_URL"] = env["REDIS_URL"].replace("localhost:6379", "localhost:56379")
print("loaded", len(env), "CI env vars (values not shown)")
os.execvpe("poetry", ["poetry", "run", "pytest", "tests/", "-v", "--cov=app",
                      "--cov-report=term"], {**os.environ, **env})
PY
```
Expected: all green, and `tests/rating/test_rating_request_concurrency.py` **runs** (not skipped). If Redis-backed 429 flakes appear, `docker exec sr-ci-redis redis-cli FLUSHDB` and re-run.

Then the `automation-flow-regression` job (its env inline, SQLite):
```bash
python3 - <<'PY'
import os, yaml
wf = yaml.safe_load(open(".github/workflows/ci.yml"))
job = wf["jobs"]["automation-flow-regression"]
step = next(s for s in job["steps"] if "pytest" in str(s.get("run", "")))
env = {k: str(v) for k, v in step["env"].items()}
os.execvpe("poetry", ["poetry", "run", "pytest", "tests/api/v1/test_chat_flow_router.py",
                      "tests/api/v1/test_flow_action_call_regressions.py", "-q"],
           {**os.environ, **env})
PY
```
Expected: green. Leave `sr-ci-pg`/`sr-ci-redis` running for Task 11; never touch `sq-*`.

- [ ] **Step 4: Reviews**

- Opus per-task reviews already run for Tasks 1, 3, 4, 6, 7, 8 (see Global Constraints). Re-confirm their findings are closed.
- One **Opus whole-branch review** (`git diff origin/develop...HEAD`) with this focus list: token never logged/returned to staff (grep the diff for `token` in log calls and staff serializers); the public POST cannot attribute without a valid token for **that** session; support-agent scoping on `/reviews/session`, `rating-status.rating`, `/agent/me/ratings`, `/analytics/agents/{id}`; lock ordering and rollback paths in `create_request`; migration upgrade/downgrade data safety; purge FK semantics. Brief the reviewer: "no AI attribution in any commit/PR; never print secrets".
- Fix findings, then re-run Steps 2–3.

- [ ] **Step 5: Verify no attribution, push, open PR**

```bash
git log origin/develop..HEAD --format=%B | grep -iE "co-authored|claude|generated with|🤖" \
  && echo "FOUND - fix before push" || echo clean
git push -u origin feat/n4-agent-rating-be
gh pr create --base develop --title "N4 agent-requested rating & review — backend" \
  --body-file /tmp/n4-pr-body.md
```
PR body (`/tmp/n4-pr-body.md`, written by hand): summary; spec + plan paths; decision IDs N4-D1…D10 and the plan's spec-conflict resolutions; migration notes (`n4agentrating`: needs `ENCRYPTION_KEY`, encrypts existing comments in place, downgrade blocked while any session has >1 rating); new endpoints; frame changes on `ticket_resolved`/`ticket_closed`; behaviour change for support agents on `GET /reviews/session`; test evidence (counts from Step 3). **No attribution footer.** Merge only on Adebayo's go, as a **merge commit**. CD deploys staging from `develop`; afterwards check `/health` and `ssh luran 'docker logs --since 15m luran_staging_api'` for the migration line.

---

### Task 11: Local live capture → FE guide, SOP, checklist

**Files:**
- Create: `docs/fe-integration-captures/agent-rating/NN-*.json`, `docs/fe-integration-guide-agent-rating.md`, `docs/sop/n4_agent_rating_backend.md`
- Modify: `docs/sop/missed_ticket_lifecycle.md` (one line: `ticket_resolved` gains `rating_request_id` + `rating_state`)
- Modify (workspace, unversioned): `/Users/adebayovictor/Documents/dev/luran/docs/checklist/luran-master-checklist.md`

Follow the screen-recording precedent: capture against the **real app run locally** (uvicorn on 127.0.0.1, Postgres 15 + Redis 7 containers, migrations at `n4agentrating`), not against fixtures. Every body in the guide is pasted from a capture file.

- [ ] **Step 1: Bring up the app locally**

```bash
docker exec sr-ci-pg dropdb -U test_user --if-exists n4live
docker exec sr-ci-pg createdb -U test_user n4live
docker exec sr-ci-redis redis-cli FLUSHDB
```
Start the API with the CI `test` env (loaded programmatically as in Task 10 Step 3) overridden with: `DATABASE_URL=…55432/n4live`, `REDIS_URL=…56379/0`, `ENVIRONMENT=development`, `AGENT_ALERT_ENABLED=false`, and a throwaway `ENCRYPTION_KEY` generated into the env without printing it. Then `poetry run alembic upgrade head` and `poetry run uvicorn app.main:app --host 127.0.0.1 --port 8765` (run it in the background; read its log for the startup line only).

- [ ] **Step 2: Seed + capture harness (scratch, not committed)**

Write `/tmp/n4_capture.py` (run with `poetry run python`, same env). It:
1. Seeds via the app models (direct DB, like the screen-recording harness): tenant "Acme Test Co" with `TenantCapabilities(can_access_analytics=True)`, an admin "Ada Admin", agents "Sam Rivers" and "Kim Lee", all `@example.com` (login rejects `.test`). Staff JWTs are minted with `app.core.security.create_access_token` exactly as the app does; they appear in captures only as `<admin-jwt-placeholder>` / `<agent-jwt-placeholder>`.
2. Uses `httpx` against `http://127.0.0.1:8765/api/v1` and `websockets` for `ws://127.0.0.1:8765/api/v1/ws/chat/{session_id}?user_identifier=…` (customer) and `/ws/agent/{session_id}?token=…` (agent), both connected **before** each action and drained for 2 s after it.
3. Writes each exchange to `docs/fe-integration-captures/agent-rating/NN-name.json` as `{"request": {method, path, headers, body}, "response": {status, body}}`, then a redaction pass replaces every JWT, every `token`/`rating_token` value, API key and the customer identifier with labelled placeholders (`<rating-token-placeholder>` etc.). Ids may stay (they are random UUIDs from a throwaway DB). Assert in the harness that no capture file contains the raw token or a JWT before finishing.

Captures (numbered in this order):
1. `01-create-session` — `POST /chat/sessions` (public).
2. `02-create-ticket` — `POST /tickets` (admin, `TicketManualCreate`).
3. `03-takeover` — `POST /chat/sessions/{sid}/takeover` (agent Sam) → ticket `in_progress`.
4. `04-rating-status-can-request` — `GET /tickets/{tid}/rating-status` (Sam).
5. `05-rating-request-201` + `06-ws-customer-rating_requested` + `07-ws-agent-rating_request_created`.
6. `08-rating-status-cooldown-open-request`; `09-rating-request-409-cooldown`.
7. `10-rating-request-403-other-agent` (Kim); `11-rating-request-404-other-tenant`; `12-rating-request-409-no-human-agent` (second unassigned ticket, admin).
8. `13-session-messages-admin-redacted` — `GET /chat/sessions/{sid}/messages` (admin) showing the notice **without** `rating_token`.
9. `14-customer-history-with-token` — `GET /customer/conversations/{sid}/messages` with a real customer login if the local customer-auth flow allows it (register + login via `customer_auth` endpoints); if email verification blocks it, mark this capture **resolved-from-code** (`customer_conversations.py`, `test_customer_history_keeps_token`) instead of faking it.
10. `15-submit-tokened-201` — `POST /reviews/session` with `rating_request_token`; `16-submit-reused-409`; `17-submit-invalid-400`; `18-submit-legacy-201` (no token, `meta.source = "agent_resolved"`); `19-submit-legacy-duplicate-409-string`; `20-submit-comment-501-422`.
11. Second ticket for Kim: `21-resolve-with-auto-request` — `POST /tickets/{tid}/resolve` (Kim) plus `22-ws-resolve-frame-order` (customer + agent frames in arrival order: `rating_requested`, `rating_request_created`, `ticket_resolved` with `pending`); `23-submit-auto-token-201`.
12. Superseded flow on a third ticket: request, then set its `created_at` back 11 min in the DB (harness, documented), request again → `24-rating-request-supersede-201`, then `25-submit-superseded-410`.
13. `26-resolve-already-rated` (ticket whose agent was rated → `rating_state: "already_rated"`); `27-close-frame-fields` (admin closes a ticket with an open request → `ticket_closed` with `pending`).
14. `28-reviews-list-admin` (filters `rated_party=human`), `29-reviews-list-support-agent-scoped`, `30-reviews-list-403-scope`.
15. `31-analytics-agents` (admin), `32-analytics-agent-detail-self` (Sam), `33-agent-me-ratings` (Sam), `34-session-detail-with-ratings` (admin, `GET /chat/sessions/{sid}`).
16. `35-submit-rate-limited-429` — only if it can be produced honestly (21 posts from one IP within the hour on the local app); otherwise mark resolved-from-code (`http_rate_limiter.rating_rate_limit`).
17. **Reload-before-submit** (spec §8 live list): the widget reload is a FE concern; capture only the BE facts it relies on — the token still validates after the customer socket reconnects (`36-submit-after-reconnect-201`), and the customer WS replay does **not** resend the ASSISTANT card (state that P2 must keep the card in localStorage).

- [ ] **Step 3: Write `docs/fe-integration-guide-agent-rating.md` from the captures only**

Sections (FE terms first): 1 What the feature is and when it triggers (manual button, auto on resolve, nothing on auto-resolver/close). 2 State machine of a request (open → fulfilled | expired | superseded) and of the widget card (`pending` / `already_rated` / `none` / field absent). 3 Every endpoint with real request/response, status codes, auth: `POST /tickets/{id}/rating-requests`, `GET /tickets/{id}/rating-status`, `POST /reviews/session` (legacy + tokened), `GET /reviews/session` (filters, scoping), `GET /analytics/agents[/{id}]`, `GET /agent/me/ratings`, `GET /chat/sessions/{id}` `ratings`. 4 WS frames verbatim (`rating_requested`, `rating_request_created`, new fields on `ticket_resolved`/`ticket_closed`) and their **order** on resolve. 5 Every error with exact `code`/`message` and the string-vs-object `detail` split on `POST /reviews/session`. 6 Field-nesting traps (spec §10): the token exists **only** in the customer frame and customer history (`meta.rating_token`), never in staff payloads; `rating_state` absent (old BE) vs `"none"`; `agent_name` is a **first name** in the customer frame but the **full name** in the 201 and in `rated_agent_name`; on `/analytics/agents` only the three rating fields are windowed (`total_tickets_resolved` is all-time); `rating` is `null` for non-rated agents; a reused request (auto-resolve while a manual request is open) sends **no** new `rating_requested` frame — `ticket_resolved` points at the existing `rating_request_id` and the card from the earlier manual frame stays live. 7 UX consequences the FE must surface: supersede makes the older card inactive ("This request was replaced"); 410 → "This rating link is no longer active."; cooldown minutes on the disabled button; support agents now see only their own ratings on the ratings page (behaviour change); `pending` with an unknown token → show nothing (never the generic prompt). 8 Polling guidance: `rating-status` on load and on window refocus + on `rating_request_created`; no polling loop. 9 Verification table: every behaviour marked verified-live (local) / resolved-from-code / not-live, with the capture number.

- [ ] **Step 4: SOP + lifecycle note**

`docs/sop/n4_agent_rating_backend.md` (match `docs/sop/screen_recording_backend.md` style): What shipped (PR #, merge SHA), Why (spec §1), How (N4-D1…D10 one line each + the plan's spec-conflict resolutions), What's involved (files table, migration `n4agentrating`, new endpoints/frames), Verification (test counts, local CI repro, live capture list), **Operate / roll back** (stop auto requests by reverting the resolve-hook call site in `tickets.py`; migration downgrade is blocked while any session has >1 rating — de-dup query `DELETE FROM session_ratings WHERE rated_agent_id IS NOT NULL AND session_id IN (…)` run only by Adebayo; `ENCRYPTION_KEY` must be present for both directions), Follow-ups (spec §12 + §11 open questions). Add one line to `docs/sop/missed_ticket_lifecycle.md`: "`ticket_resolved` / `ticket_closed` now also carry `rating_request_id` and `rating_state` (N4, see `n4_agent_rating_backend.md`)."

- [ ] **Step 5: Checklist**

In the workspace master checklist, replace the single unchecked N4 line in section D with an "N4 agent rating" block: P1 BE tasks 1–11 (checked only when verified, with PR ref), P2 `luran-chat` and P3 dashboard + app unchecked with "plan pending (written from the live guide)", and deferred items (CSAT tile on overview, customer-deletion scrub of `session_ratings`, tenant toggle Q6, rating-received notification). Add a dated changelog row.

- [ ] **Step 6: Commit + push**

```bash
docker exec sr-ci-pg dropdb -U test_user n4live
git add docs/fe-integration-guide-agent-rating.md docs/fe-integration-captures/agent-rating docs/sop/n4_agent_rating_backend.md docs/sop/missed_ticket_lifecycle.md
git commit -m "docs(ratings): live-captured N4 FE guide, backend SOP, lifecycle note"
git push
```
(The workspace checklist is outside this repo; it is saved, not committed.)

---

## Self-Review

**1. Spec coverage**

| Spec item | Task |
|---|---|
| §4.1 `rating_requests` table + indexes; `session_ratings` columns, CHECKs, uniqueness swap, tenant/agent index, backref | 1 |
| §4.1 backfill `source`, keep `rated_agent_id` NULL, encrypt comments, guarded downgrade | 1 |
| §4.2 `create_request` steps 1–6, first-name fallback, concurrency | 2 |
| §4.3.1 `POST /tickets/{id}/rating-requests` (201 / 404 / 403 / 409×4) | 4 |
| §4.3.2 `GET /tickets/{id}/rating-status` incl. `rating` visibility | 4 |
| §4.3.3 public POST token path, 500-char comment, `http_rating` 20/h → 429, legacy unchanged | 6 |
| §4.3.4 list filters, `csat_percent`, enrichment, support-agent scope, platform `/ratings/all` items | 7 |
| §4.3.5 `GET /agent/me/ratings` | 8 |
| §4.3.6 analytics per-agent fields, one grouped query, rounding | 8 |
| §4.3.7 `SessionDetailResponse.ratings` | 3 |
| §4.4 resolve hook (own rollback, ordering, `rating_state`, `TicketResponse.rating_request_id`), close fields, auto-resolver untouched | 5 |
| §4.5 frames | 4, 5 |
| §4.6 staff redaction + WS replay pin + "every staff endpoint" test | 3 (+ 4: 201/status bodies carry no token) |
| §4.7 permissions table | 4, 6, 7, 8 |
| N4-D10 purge | 9 |
| §8 tests, local CI | 1–10 |
| §8 live list / §10 docs (guide, SOP, checklist) | 11 |

No gaps found.

**Spec conflicts resolved in this plan (also listed in the PR body and SOP):**
1. **`visitor_service.py:517` is not staff-facing.** It builds the *public* visitor bootstrap response (customer's own last conversation). It is left **unredacted**, like `customer_conversations.py`; redacting it would strip the token from the hosted support page's own history.
2. **Reuse on auto-resolve sends no `rating_requested` frame (§4.2 step 4 wins over §4.4 step 2).** The server stores only the token hash, so a reused request's token cannot be re-sent; `ticket_resolved` carries `rating_state: "pending"` + the existing `rating_request_id`, and the widget keeps the card it got from the manual frame.
3. **The rating message is inserted directly in the same transaction as the request**, not through `ConversationService.add_message`, which commits mid-lock. Same content/meta/role and the same `last_message_at` bump.
4. **Viewers on `GET /reviews/session`** (spec silent) are scoped like support agents (own only → effectively empty).
5. **Object 429 for `rating_rate_limited`** needs a wrapper (`rating_rate_limit`), because the shared limiter's 429 body is `{"error": "Rate limit exceeded", …}`.
6. **`rating-status.rating`** = the human rating for (session, current assignee).
7. **`ix_rating_requests_session_agent_created`** is ascending, not `created_at DESC`: Postgres scans a btree backwards for `ORDER BY created_at DESC LIMIT 1`, and a plain column index keeps model and migration identical.
8. **404 (not 403) for another tenant's ticket** on the two new ticket endpoints (as the spec table says; existing ticket routes use `verify_tenant_access` → 403).

**2. Placeholder scan.** No TBD/TODO. The two runtime-variable steps (Task 1 Step 7 seed, Task 11 capture harness) give the exact script/approach and the exact checks; their *output* is data, not plan content.

**3. Type consistency.** `create_request(ticket_id, *, source, requested_by)` is used the same way in Tasks 2, 3, 4, 5, 6, 7, 8. `RatingRequestResult.created/token/message/agent_first_name/agent_name` are used consistently by `fanout_rating_request` and the 201 builder. `rating_state(ticket) -> (state, request_id)` vs `auto_request_on_resolve(...) -> (request_id, state)` is deliberate and both callers unpack in that order. `SessionRatingSubmitResponse` (POST) ⊂ `SessionRatingResponse` (lists). `RatingStats.average` maps to `average_rating` (analytics) and `average_score` (`/agent/me/ratings`, list), matching each endpoint's existing naming.

**4. Review Focus.** The five lines map to: Task 6 `test_token_rating_attributes_to_request_agent_after_reassignment`, Task 5 `test_request_failure_never_fails_resolve`, Task 6 `test_legacy_resolution_rating_and_human_rating_coexist`, Task 4 `test_status_rating_visible_only_to_admin_and_rated_agent`, Task 7 `test_legacy_long_comment_still_lists`. All present.
