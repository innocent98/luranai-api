# Screen Recording + Attachment Retention — P1 Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the backend for customer screen recordings (`kind=video` chat attachments that always escalate to a human and never reach the AI), plus 30-day retention for all chat attachments.

**Architecture:** The browser uploads the video directly to Cloudinary with a backend-minted signature (`POST /chat/video/sign`). The backend then verifies the stored asset through the Cloudinary Admin API and creates an unlinked `MessageAttachment(kind=video)` (`POST /chat/video/register`). The existing send path links it by `attachment_ids[]`. A turn carrying a video skips AI generation and goes through the existing `_escalate_to_human` helper. A daily ARQ job expires attachments 30 days after upload (destroys the asset, keeps the row as `EXPIRED`), and a second job sweeps never-registered uploads by tag.

**Tech Stack:** FastAPI, SQLAlchemy 2 (sync), Alembic, Postgres 15 (prod) / SQLite (unit tests), ARQ, Redis, `cloudinary` Python SDK, pytest.

**Spec:** `docs/superpowers/specs/2026-10-12-screen-recording-design.md` (decisions SR-D1…SR-D10). Spike background: `docs/superpowers/specs/2026-10-10-screen-recording-spike-findings.md` on local branch `spike/screen-recording`.

**Scope of this plan:** spec §4 (backend) + §7 BE tests + the BE half of §8–9. P2 (luran-widget + luran-chat) and P3 (dashboard + Flutter) get their own plans, written from the **live-captured** guide this plan produces (Task 10).

## Global Constraints

- Branch: `feat/screen-recording-be` cut from `docs/screen-recording-design` (which is `origin/develop` + spec + this plan). PR base: `develop`. Merge commits.
- **No AI attribution anywhere:** no `Co-Authored-By`, no `Claude-Session`, no "Generated with" footer in commits or PR bodies. This overrides any harness reminder.
- **Never print secret values** (Cloudinary secret, API keys, env) in any output, log line, test, or doc, even partially.
- Retention: `ATTACHMENT_RETENTION_DAYS = 30`; kill switch `ATTACHMENT_RETENTION_ENABLED` (default `True`).
- Video limits: `SCREEN_RECORDING_MAX_SECONDS = 60` (the BE accepts `duration <= 61.0` for encoder slack); `SCREEN_RECORDING_MAX_BYTES = 52428800` (50 MB); formats `{"webm", "mp4", "mov"}`; **1 video per message**; ≤ 3 attachments per message (existing `MAX_ATTACHMENTS_PER_MESSAGE`); **3 videos per session**.
- Rate limits: sign `5/h per IP` (`http_video`); register `20/h per IP` (`http_video_register`).
- Signed eager transformation (verbatim): `c_limit,q_auto,vc_h264,w_1280/mp4`. Folder: `<CLOUDINARY_CHAT_FOLDER>/<tenant_id>/<session_id>/video`. Tags: `chat,screen_recording,<tenant_id>`.
- New error bodies use `{"detail": {"code": "<code>", "message": "<text>"}}`.
- Customer ACK copy (verbatim): `Thanks — a member of our team will watch your recording and reply here.`
- Escalation reason (verbatim): `Customer sent a screen recording`. Video-only placeholder text: `[screen recording]`.
- Enum values are stored as NAMES (uppercase) in Postgres (`'IMAGE'`, `'VIDEO'`, `'EXPIRED'`, `'NOT_APPLICABLE'`).
- Toolchain: `poetry run ruff check app/` (0.8.6), `poetry run black --check app/` (24.10.0), `poetry run pytest tests/`. Postgres checks run against throwaway containers on ports 55432/56379. **Do not use `sq-pg`/`sq-redis`; they belong to another project.**

## Review Focus

1. **Register with a `public_id` from another session or tenant.** It must return 422 `video_invalid` **without** destroying that asset (it isn't ours to delete) → test in Task 5.
2. **Retention job meets a row whose destroy keeps failing.** It must not loop forever within one run, must leave the row LINKED, and must move on → test in Task 7.
3. **A video sent into an already-escalated session whose ticket is IN_PROGRESS.** Persist it and fan it out, but no ACK, no re-ring, no new ticket → test in Task 6.
4. **Expired attachments serialized through every path** (REST `AttachmentOut`, WS `attachment_frames`, agent-app history). They must give `url: null`, `expired: true` and never raise → test in Task 2.
5. **A double `register` for the same `public_id`** (client retry after a network drop). The second call returns 409 with the original `attachment_id` and creates no second row → test in Task 5.

---

## File Structure

| File | Responsibility | Action |
|---|---|---|
| `app/core/config.py` | Retention + video limit settings | Modify |
| `app/db/models/conversation.py` | `AttachmentKind`, new enum values, new attachment columns | Modify |
| `app/db/models/tenant.py` | `TenantCapabilities.can_use_screen_recording`, `Tenant.widget_allow_screen_recording` | Modify |
| `alembic/versions/sr10_screen_recording.py` | Migration + image backfill | Create |
| `app/schemas/conversation.py` | `AttachmentOut` gains `kind`, `duration_ms`, `poster_url`, `expired`; nullable `url` | Modify |
| `app/services/conversation_service.py` | `attachment_frames` mirrors `AttachmentOut` | Modify |
| `app/utils/cloudinary_client.py` | `destroy(resource_type=…)`, `sign_video_upload`, `get_video_resource`, `video_delivery_urls`, `list_tagged_videos` | Modify |
| `app/workers/attachment_cleanup.py`, `app/services/account_purge_service.py` | Pass `resource_type` | Modify |
| `app/services/screen_recording/__init__.py`, `gating.py` | `screen_recording_allowed(db, tenant)` | Create |
| `app/schemas/widget.py`, `visitor.py`, `customer_conversation.py`, `tenant.py` | Expose the gate / toggle / capability | Modify |
| `app/api/v1/endpoints/widget.py`, `customer_conversations.py`, `app/services/visitor_service.py` | Fill the new fields | Modify |
| `app/utils/http_rate_limiter.py` | `chat_video_sign_rate_limit`, `chat_video_register_rate_limit` | Modify |
| `app/api/v1/endpoints/chat_video.py` | `POST /chat/video/sign`, `POST /chat/video/register` | Create |
| `app/api/v1/api.py` | Mount `chat_video.router` under `/chat` | Modify |
| `app/api/v1/endpoints/chat.py` | One-video validation, video-turn branch, AI exclusion | Modify |
| `app/services/first_responder.py` | `SCREEN_RECORDING_ACK`, `SCREEN_RECORDING_REASON` | Modify |
| `app/workers/attachment_retention.py` | `expire_attachments`, `sweep_unregistered_videos` | Create |
| `app/workers/document_processor.py` | Register the two crons | Modify |
| Tests | `tests/migrations/test_sr10_backfill.py`, `tests/api/v1/test_attachment_serialization.py`, `tests/utils/test_cloudinary_video.py`, `tests/services/test_screen_recording_gate.py`, `tests/api/v1/test_chat_video_endpoints.py`, `tests/api/v1/test_chat_video_send.py`, `tests/workers/test_attachment_retention.py` | Create |
| Docs | `docs/fe-integration-guide-screen-recording.md`, `docs/sop/screen_recording_backend.md`, `docs/sop/attachment_retention.md`, update `docs/fe-integration-guide-chat-image-upload.md`, workspace checklist | Create/Modify |

---

### Task 1: Settings, models, migration

**Files:**
- Modify: `app/core/config.py` (near `CLOUDINARY_CHAT_FOLDER`, ~line 187)
- Modify: `app/db/models/conversation.py:184-347`
- Modify: `app/db/models/tenant.py` (`Tenant` near `widget_allow_file_upload` ~:157; `TenantCapabilities` after `can_use_voice` ~:448)
- Create: `alembic/versions/sr10_screen_recording.py`
- Test: `tests/migrations/test_sr10_backfill.py`, `tests/db/test_message_attachment_defaults.py`

**Interfaces:**
- Produces: `AttachmentKind.IMAGE|VIDEO`; `AttachmentStatus.EXPIRED`; `AttachmentModeration.NOT_APPLICABLE`; `MessageAttachment.kind/duration_ms/poster_url/source_format/expires_at`, `url: str | None`; `TenantCapabilities.can_use_screen_recording: bool`; `Tenant.widget_allow_screen_recording: bool`; `settings.ATTACHMENT_RETENTION_DAYS/ATTACHMENT_RETENTION_ENABLED/SCREEN_RECORDING_MAX_SECONDS/SCREEN_RECORDING_MAX_BYTES`; migration constant `BACKFILL_SQL`.

- [ ] **Step 1: Write the failing tests**

`tests/db/test_message_attachment_defaults.py`:
```python
"""MessageAttachment defaults for kind and expires_at."""

from datetime import UTC, datetime, timedelta

from app.db.models.conversation import AttachmentKind, AttachmentStatus
from tests.api.v1 import test_chat_image_send as img


def test_new_attachment_defaults_to_image_and_30_day_expiry(db):
    sid, tid = img._session(db)
    before = datetime.now(UTC)
    aid = img._att(db, sid, tid)
    from app.db.models.conversation import MessageAttachment

    row = db.get(MessageAttachment, aid)
    assert row.kind == AttachmentKind.IMAGE
    assert row.status == AttachmentStatus.UPLOADED
    exp = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=UTC)
    assert before + timedelta(days=30) - timedelta(minutes=1) <= exp
    assert exp <= datetime.now(UTC) + timedelta(days=30, minutes=1)
```

`tests/migrations/test_sr10_backfill.py`:
```python
"""sr10 backfill: existing rows without expires_at get rollout + 30 d."""

import importlib.util
import os
from datetime import UTC, datetime, timedelta

_MIGRATION = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "alembic",
    "versions",
    "sr10_screen_recording.py",
)


def _mod():
    spec = importlib.util.spec_from_file_location("sr10_screen_recording", _MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_backfill_sql_only_fills_missing_values():
    sql = " ".join(_mod().BACKFILL_SQL.split())
    assert sql == (
        "UPDATE message_attachments SET expires_at = :expires_at WHERE expires_at IS NULL"
    )


def test_grace_is_30_days_from_now():
    mod = _mod()
    before = datetime.now(UTC)
    got = mod.backfill_expiry()
    assert before + timedelta(days=30) <= got <= datetime.now(UTC) + timedelta(days=30)
```
> Unit tests build the schema from the models (`nullable=False`), so the backfill cannot run against real NULL rows in SQLite. Its behaviour on real rows is checked twice: in the Step 7 Postgres round-trip (seeded row) and on the staging deploy (Task 10, Step 1.0).

- [ ] **Step 2: Run tests to verify they fail**

Run: `poetry run pytest tests/db/test_message_attachment_defaults.py tests/migrations/test_sr10_backfill.py -v`
Expected: FAIL (`ImportError: cannot import name 'AttachmentKind'`, migration file missing).

- [ ] **Step 3: Settings**

In `app/core/config.py`, directly under `CLOUDINARY_CHAT_FOLDER: str = "luran/chat_images"`:
```python
    # Chat attachment retention (screen recording spec SR-D5) + video limits (SR-D10)
    ATTACHMENT_RETENTION_DAYS: int = 30
    ATTACHMENT_RETENTION_ENABLED: bool = True
    SCREEN_RECORDING_MAX_SECONDS: int = 60
    SCREEN_RECORDING_MAX_BYTES: int = 52_428_800
```

- [ ] **Step 4: Models**

`app/db/models/conversation.py`: add `from datetime import datetime, timedelta`, `from app.core.config import settings`, `from app.utils.datetime import utcnow_aware`. Then:
```python
class AttachmentKind(str, enum.Enum):
    """What the attachment is; drives rendering and AI exclusion."""

    IMAGE = "image"
    VIDEO = "video"


class AttachmentStatus(str, enum.Enum):
    """Attachment lifecycle: uploaded (orphan) -> linked to a message -> expired (asset deleted)."""

    UPLOADED = "uploaded"
    LINKED = "linked"
    EXPIRED = "expired"


class AttachmentModeration(str, enum.Enum):
    """Moderation outcome (only passing images are stored; video is human-reviewed)."""

    PASSED = "passed"
    NOT_APPLICABLE = "not_applicable"


def _default_attachment_expiry() -> datetime:
    return utcnow_aware() + timedelta(days=settings.ATTACHMENT_RETENTION_DAYS)
```
On `MessageAttachment`: update the docstring to `"""Chat attachment (image or video) stored on Cloudinary; URLs encrypted."""`; change `url` to `Mapped[str | None]` with `nullable=True`, comment `"Delivery URL — encrypted at rest; NULL once expired"`; add after `content_type`:
```python
    kind: Mapped[AttachmentKind] = mapped_column(
        SQLEnum(AttachmentKind, name="attachment_kind"),
        nullable=False,
        default=AttachmentKind.IMAGE,
        server_default="IMAGE",
    )
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    poster_url: Mapped[str | None] = mapped_column(
        EncryptedString, nullable=True, comment="Video poster URL — encrypted at rest"
    )
    source_format: Mapped[str | None] = mapped_column(String(16), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_default_attachment_expiry,
        index=True,
        comment="Asset is destroyed after this (retention, SR-D5)",
    )
```
and add to the class:
```python
    __table_args__ = (
        Index(
            "uq_message_attachments_video_public_id",
            "cloudinary_public_id",
            unique=True,
            postgresql_where=text("kind = 'VIDEO'"),
            sqlite_where=text("kind = 'VIDEO'"),
        ),
    )
```
(import `text` from `sqlalchemy`). The unique index is **partial (video only)** so the many existing image tests that reuse `"chat/x"` keep passing.

`app/db/models/tenant.py`: beside `widget_allow_file_upload`:
```python
    widget_allow_screen_recording: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
        server_default="true",
        comment="Tenant's own on/off for customer screen recordings (grant: TenantCapabilities)",
    )
```
After `can_use_voice` on `TenantCapabilities`:
```python
    can_use_screen_recording: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
        server_default="true",
        comment="Platform grant for customer screen recordings (on for every plan today)",
    )
```
> Deviation from spec §4.1, recorded here: the capability is **not** added to the plan config map. Like `can_use_voice`, it is a platform grant defaulting to true and revocable per tenant through the platform `TenantCapabilitiesUpdate` endpoint. Plan sync only writes keys present in the plan config, so it never resets this one. Selling it as a tier feature later means adding the key to plan configs.

- [ ] **Step 5: Migration**

`alembic/versions/sr10_screen_recording.py`:
```python
"""screen recording: attachment kind/video columns, retention, gate flags

Revision ID: sr10screenrec
Revises: n3firstresp
Create Date: 2026-10-12

Adds kind (IMAGE|VIDEO), duration_ms, poster_url, source_format, expires_at to
message_attachments; url becomes nullable (NULL once expired); EXPIRED status and
NOT_APPLICABLE moderation values; partial unique index on video public ids;
tenant gate columns. Existing rows get expires_at = migration time + 30 d
(SR-D6: 30-day grace from rollout). Enum values are stored as NAMES.
"""

from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ENUM

revision = "sr10screenrec"
down_revision = "n3firstresp"
branch_labels = None
depends_on = None

BACKFILL_SQL = "UPDATE message_attachments SET expires_at = :expires_at WHERE expires_at IS NULL"


def backfill_expiry() -> datetime:
    return datetime.now(UTC) + timedelta(days=30)


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # Additive enum values; PG15 allows ADD VALUE in a txn when unused in that txn.
    op.execute("ALTER TYPE attachment_status ADD VALUE IF NOT EXISTS 'EXPIRED'")
    op.execute("ALTER TYPE attachment_moderation ADD VALUE IF NOT EXISTS 'NOT_APPLICABLE'")
    kind = ENUM("IMAGE", "VIDEO", name="attachment_kind", create_type=False)
    kind.create(op.get_bind(), checkfirst=True)

    op.add_column(
        "message_attachments",
        sa.Column("kind", kind, nullable=False, server_default="IMAGE"),
    )
    op.add_column("message_attachments", sa.Column("duration_ms", sa.Integer(), nullable=True))
    op.add_column("message_attachments", sa.Column("poster_url", sa.Text(), nullable=True))
    op.add_column(
        "message_attachments", sa.Column("source_format", sa.String(length=16), nullable=True)
    )
    op.add_column(
        "message_attachments",
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.get_bind().execute(sa.text(BACKFILL_SQL), {"expires_at": backfill_expiry()})
    op.alter_column("message_attachments", "expires_at", nullable=False)
    op.alter_column("message_attachments", "url", existing_type=sa.Text(), nullable=True)
    op.create_index(
        "ix_message_attachments_expires_at", "message_attachments", ["expires_at"]
    )
    op.create_index(
        "uq_message_attachments_video_public_id",
        "message_attachments",
        ["cloudinary_public_id"],
        unique=True,
        postgresql_where=sa.text("kind = 'VIDEO'"),
    )

    op.add_column(
        "tenant_capabilities",
        sa.Column(
            "can_use_screen_recording", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
    )
    op.add_column(
        "tenants",
        sa.Column(
            "widget_allow_screen_recording",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )


def downgrade() -> None:
    # Video rows and expired rows cannot be represented pre-sr10. Their Cloudinary
    # assets are NOT destroyed here: run sweep_unregistered_videos / purge first if needed.
    op.execute("DELETE FROM message_attachments WHERE kind = 'VIDEO' OR url IS NULL")
    op.drop_column("tenants", "widget_allow_screen_recording")
    op.drop_column("tenant_capabilities", "can_use_screen_recording")
    op.drop_index("uq_message_attachments_video_public_id", table_name="message_attachments")
    op.drop_index("ix_message_attachments_expires_at", table_name="message_attachments")
    op.alter_column("message_attachments", "url", existing_type=sa.Text(), nullable=False)
    op.drop_column("message_attachments", "expires_at")
    op.drop_column("message_attachments", "source_format")
    op.drop_column("message_attachments", "poster_url")
    op.drop_column("message_attachments", "duration_ms")
    op.drop_column("message_attachments", "kind")
    ENUM(name="attachment_kind").drop(op.get_bind(), checkfirst=True)
    # EXPIRED / NOT_APPLICABLE enum values stay (Postgres cannot drop enum values).
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `poetry run pytest tests/db/test_message_attachment_defaults.py tests/migrations/test_sr10_backfill.py tests/api/v1/test_chat_image_send.py tests/api/v1/test_chat_image_upload.py tests/workers/test_attachment_cleanup.py -v`
Expected: PASS (existing image tests unaffected).

- [ ] **Step 7: Migration round-trip on Postgres**

```bash
docker run -d --rm --name sr10-pg -e POSTGRES_USER=u -e POSTGRES_PASSWORD=p -e POSTGRES_DB=d -p 55432:5432 postgres:15-alpine
sleep 4
export DATABASE_URL=postgresql://u:p@localhost:55432/d
poetry run alembic upgrade n3firstresp
# seed one pre-sr10 image row (tenant -> session -> attachment; fill every NOT NULL column
# the n3firstresp schema requires; inspect with \d tenants etc.)
docker exec -i sr10-pg psql -U u -d d < scripts/sql/sr10_seed_image_row.sql
poetry run alembic upgrade head
docker exec sr10-pg psql -U u -d d -c "SELECT kind, expires_at > now() + interval '29 days' AS graced FROM message_attachments"
poetry run alembic downgrade -1
poetry run alembic upgrade head
docker stop sr10-pg
```
Expected: every command exits 0; the SELECT returns `IMAGE | t`. Write `scripts/sql/sr10_seed_image_row.sql` as a throwaway helper and **do not commit it**. (If the app's alembic env needs further env vars, load them from `.github/workflows/ci.yml` programmatically. Do not echo them.)

- [ ] **Step 8: Commit**

```bash
git add app/core/config.py app/db/models/conversation.py app/db/models/tenant.py alembic/versions/sr10_screen_recording.py tests/db/test_message_attachment_defaults.py tests/migrations/test_sr10_backfill.py
git commit -m "feat(attachments): kind/video columns, 30-day expires_at, screen-recording gate flags (sr10)"
```

---

### Task 2: Attachment serialization (REST + WS)

**Files:**
- Modify: `app/schemas/conversation.py:101-110` (`AttachmentOut`)
- Modify: `app/services/conversation_service.py:656-669` (`attachment_frames`)
- Modify: `app/api/v1/endpoints/chat.py:186-194` (`_upload_response`)
- Test: `tests/api/v1/test_attachment_serialization.py`

**Interfaces:**
- Consumes: Task 1 columns.
- Produces: `AttachmentOut{id, url: str|None, content_type, width, height, kind: "image"|"video", duration_ms: int|None, poster_url: str|None, expired: bool}`; `attachment_frames(attachments) -> list[dict]` with the same keys; `_upload_response(att) -> dict` adds `kind`, `duration_ms`, `poster_url`.

- [ ] **Step 1: Write the failing test**

```python
"""AttachmentOut / attachment_frames carry kind, video fields and expired."""

from app.db.models.conversation import (
    AttachmentKind,
    AttachmentStatus,
    MessageAttachment,
)
from app.schemas.conversation import AttachmentOut
from app.services.conversation_service import attachment_frames
from tests.api.v1 import test_chat_image_send as img


def _row(db, **kw):
    sid, tid = img._session(db)
    aid = img._att(db, sid, tid)
    row = db.get(MessageAttachment, aid)
    for k, v in kw.items():
        setattr(row, k, v)
    db.commit()
    db.refresh(row)
    return row


def test_image_defaults(db):
    out = AttachmentOut.model_validate(_row(db))
    assert out.kind == "image"
    assert out.expired is False
    assert out.duration_ms is None and out.poster_url is None


def test_video_fields(db):
    row = _row(
        db,
        kind=AttachmentKind.VIDEO,
        duration_ms=42000,
        poster_url="https://c/p.jpg",
        content_type="video/mp4",
    )
    out = AttachmentOut.model_validate(row)
    assert (out.kind, out.duration_ms, out.poster_url) == ("video", 42000, "https://c/p.jpg")
    frame = attachment_frames([row])[0]
    assert frame["kind"] == "video" and frame["duration_ms"] == 42000
    assert frame["poster_url"] == "https://c/p.jpg" and frame["expired"] is False


def test_expired_has_null_urls_everywhere(db):
    row = _row(db, status=AttachmentStatus.EXPIRED, url=None, poster_url=None)
    out = AttachmentOut.model_validate(row)
    assert out.expired is True and out.url is None and out.poster_url is None
    frame = attachment_frames([row])[0]
    assert frame["expired"] is True and frame["url"] is None
```

- [ ] **Step 2: Run to verify failure**

Run: `poetry run pytest tests/api/v1/test_attachment_serialization.py -v`
Expected: FAIL (`AttachmentOut` has no `kind`).

- [ ] **Step 3: Implement**

`app/schemas/conversation.py`:
```python
class AttachmentOut(BaseModel):
    """Chat attachment surfaced on a message (image or video; url is null once expired)."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    url: str | None = None
    content_type: str
    width: int | None = None
    height: int | None = None
    kind: str = "image"
    duration_ms: int | None = None
    poster_url: str | None = None
    expired: bool = False

    @model_validator(mode="before")
    @classmethod
    def _from_row(cls, data):  # type: ignore[no-untyped-def]
        # ORM rows: derive the wire kind/expired from the enums.
        if hasattr(data, "status") and hasattr(data, "kind"):
            status = getattr(data.status, "value", data.status)
            kind = getattr(data.kind, "value", data.kind)
            return {
                "id": data.id,
                "url": data.url,
                "content_type": data.content_type,
                "width": data.width,
                "height": data.height,
                "kind": kind,
                "duration_ms": data.duration_ms,
                "poster_url": data.poster_url,
                "expired": status == "expired",
            }
        return data
```
(`model_validator` is already imported in this module; check that `MessageRequest` uses it.)

`app/services/conversation_service.py`: replace the body of `attachment_frames`:
```python
def attachment_frames(attachments) -> list[dict]:
    """WS payload shape for a message's attachments (same fields as REST
    `AttachmentOut`), in send order. Every frame carrying a `message_id` must
    include this so clients that merge by id never drop attachments."""
    return [
        {**AttachmentOut.model_validate(a).model_dump(mode="json")} for a in attachments
    ]
```
(import `AttachmentOut` from `app.schemas.conversation` if it is not already imported. `model_dump(mode="json")` stringifies the UUID, matching the previous `str(a.id)`.)

`app/api/v1/endpoints/chat.py` `_upload_response`: add
```python
        "kind": getattr(attachment.kind, "value", attachment.kind),
        "duration_ms": attachment.duration_ms,
        "poster_url": attachment.poster_url,
```

- [ ] **Step 4: Run the new test and the existing WS/agent-app suites**

Run: `poetry run pytest tests/api/v1/test_attachment_serialization.py tests/api/v1/test_chat_image_send.py tests/api/v1/test_agent_image_message.py tests/api/v1 -k "attach or image or agent_app" -v`
Expected: PASS. If an existing assertion compares a frame dict with `==` to the old 5-key shape, update that assertion to the new keys (that is the contract change). Do not weaken it.

- [ ] **Step 5: Commit**

```bash
git add app/schemas/conversation.py app/services/conversation_service.py app/api/v1/endpoints/chat.py tests/
git commit -m "feat(attachments): serialize kind, video fields and expired across REST and WS"
```

---

### Task 3: Cloudinary client (video-aware destroy, sign, verify, URLs, tag listing)

**Files:**
- Modify: `app/utils/cloudinary_client.py:284-296` (+ new methods)
- Modify: `app/workers/attachment_cleanup.py:47`, `app/services/account_purge_service.py:362-375`
- Modify: `tests/workers/test_attachment_cleanup.py` (positional `destroy` assertions), `tests/workers/test_account_deletion_purge.py` (if it asserts destroy calls)
- Test: `tests/utils/test_cloudinary_video.py`

**Interfaces:**
- Produces:
  - `CloudinaryManager.destroy(public_id: str, *, resource_type: str) -> tuple[bool, str | None]` (keyword **required**; "not found" counts as success)
  - `sign_video_upload(tenant_id: UUID, session_id: UUID) -> dict | None`, returning `{"upload_url", "fields": {...}, "public_id": "<folder>/<hex>"}`
  - `video_folder(tenant_id, session_id) -> str`
  - `get_video_resource(public_id: str) -> dict | None`
  - `video_delivery_urls(public_id: str, version: int | str | None) -> tuple[str, str]` (mp4, poster)
  - `list_tagged_videos(tag: str, next_cursor: str | None) -> tuple[list[dict], str | None]`
  - Module constant `VIDEO_EAGER = "c_limit,q_auto,vc_h264,w_1280/mp4"`

- [ ] **Step 1: Write the failing tests**

`tests/utils/test_cloudinary_video.py`:
```python
"""Video helpers on CloudinaryManager. The SDK is mocked; no network."""

from unittest.mock import patch
from uuid import uuid4

import cloudinary.utils
import pytest

from app.utils import cloudinary_client as cc


@pytest.fixture
def mgr(monkeypatch):
    m = cc.CloudinaryManager.__new__(cc.CloudinaryManager)
    m.enabled = True
    monkeypatch.setattr(cc.settings, "CLOUDINARY_API_SECRET", "test-secret-not-real")
    monkeypatch.setattr(cc.settings, "CLOUDINARY_API_KEY", "test-key")
    monkeypatch.setattr(cc.settings, "CLOUDINARY_CLOUD_NAME", "demo")
    monkeypatch.setattr(cc.settings, "CLOUDINARY_CHAT_FOLDER", "luran/chat_images")
    return m


def test_destroy_passes_resource_type(mgr):
    with patch.object(cc.cloudinary.uploader, "destroy", return_value={"result": "not found"}) as d:
        assert mgr.destroy("a/b", resource_type="video") == (True, None)
    d.assert_called_once_with("a/b", resource_type="video", invalidate=True)


def test_destroy_requires_resource_type(mgr):
    with pytest.raises(TypeError):
        mgr.destroy("a/b")  # type: ignore[call-arg]


def test_sign_video_upload_fields_and_signature(mgr):
    t, s = uuid4(), uuid4()
    out = mgr.sign_video_upload(t, s)
    f = out["fields"]
    assert out["upload_url"] == "https://api.cloudinary.com/v1_1/demo/video/upload"
    assert f["folder"] == f"luran/chat_images/{t}/{s}/video"
    assert f["eager"] == cc.VIDEO_EAGER and f["eager_async"] == "true"
    assert f["tags"] == f"chat,screen_recording,{t}"
    assert out["public_id"] == f"{f['folder']}/{f['public_id']}"
    signed = {k: f[k] for k in ("timestamp", "folder", "public_id", "eager", "eager_async", "tags")}
    assert f["signature"] == cloudinary.utils.api_sign_request(signed, "test-secret-not-real")
    tampered = {**signed, "folder": "luran/chat_images/other/video"}
    assert f["signature"] != cloudinary.utils.api_sign_request(tampered, "test-secret-not-real")
    assert "test-secret-not-real" not in repr(out)


def test_get_video_resource_not_found_returns_none(mgr):
    with patch.object(cc.cloudinary.api, "resource", side_effect=cc.cloudinary.exceptions.NotFound("x")):
        assert mgr.get_video_resource("a/b") is None


def test_delivery_urls(mgr):
    mp4, poster = mgr.video_delivery_urls("luran/chat_images/t/s/video/abc", 123)
    assert mp4.startswith("https://res.cloudinary.com/demo/video/upload/")
    assert "vc_h264" in mp4 and "w_1280" in mp4 and mp4.endswith("/v123/luran/chat_images/t/s/video/abc.mp4")
    assert "so_0" in poster and "w_640" in poster and poster.endswith(".jpg")


def test_list_tagged_videos(mgr):
    page = {"resources": [{"public_id": "x", "created_at": "2026-10-10T00:00:00Z"}], "next_cursor": "c2"}
    with patch.object(cc.cloudinary.api, "resources_by_tag", return_value=page) as r:
        res, cursor = mgr.list_tagged_videos("screen_recording", None)
    assert res[0]["public_id"] == "x" and cursor == "c2"
    r.assert_called_once_with("screen_recording", resource_type="video", max_results=100)
```

- [ ] **Step 2: Run to verify failure**

Run: `poetry run pytest tests/utils/test_cloudinary_video.py -v`
Expected: FAIL (`VIDEO_EAGER` missing; `destroy` signature).

- [ ] **Step 3: Implement** in `app/utils/cloudinary_client.py` (add `import time`, `import cloudinary.api`, `import cloudinary.exceptions`, `import cloudinary.utils`):

```python
VIDEO_EAGER = "c_limit,q_auto,vc_h264,w_1280/mp4"
_VIDEO_TRANSFORM = [{"crop": "limit", "quality": "auto", "video_codec": "h264", "width": 1280}]
_POSTER_TRANSFORM = [{"start_offset": "0", "width": 640, "crop": "limit"}]
```
Methods on `CloudinaryManager`:
```python
    def video_folder(self, tenant_id: UUID, session_id: UUID) -> str:
        return f"{settings.CLOUDINARY_CHAT_FOLDER}/{tenant_id}/{session_id}/video"

    def sign_video_upload(self, tenant_id: UUID, session_id: UUID) -> dict | None:
        """Signed direct-upload payload for a screen recording (secret never leaves the server).

        Every signed field is tamper-proof: folder scopes the asset to this
        tenant/session; tags let the unregistered-upload sweep find it.
        """
        if not self.enabled:
            return None
        to_sign = {
            "timestamp": int(time.time()),
            "folder": self.video_folder(tenant_id, session_id),
            "public_id": uuid4().hex,
            "eager": VIDEO_EAGER,
            "eager_async": "true",
            "tags": f"chat,screen_recording,{tenant_id}",
        }
        signature = cloudinary.utils.api_sign_request(to_sign, settings.CLOUDINARY_API_SECRET)
        return {
            "upload_url": (
                f"https://api.cloudinary.com/v1_1/{settings.CLOUDINARY_CLOUD_NAME}/video/upload"
            ),
            "fields": {**to_sign, "api_key": settings.CLOUDINARY_API_KEY, "signature": signature},
            "public_id": f"{to_sign['folder']}/{to_sign['public_id']}",
        }

    def get_video_resource(self, public_id: str) -> dict | None:
        """Admin API lookup of an uploaded video; None if it does not exist."""
        try:
            return cloudinary.api.resource(public_id, resource_type="video")
        except cloudinary.exceptions.NotFound:
            return None

    def video_delivery_urls(self, public_id: str, version: int | str | None) -> tuple[str, str]:
        """(progressive mp4 matching the signed eager, jpg poster at 0 s)."""
        mp4, _ = cloudinary.utils.cloudinary_url(
            public_id, resource_type="video", format="mp4", secure=True,
            version=version, transformation=_VIDEO_TRANSFORM,
        )
        poster, _ = cloudinary.utils.cloudinary_url(
            public_id, resource_type="video", format="jpg", secure=True,
            version=version, transformation=_POSTER_TRANSFORM,
        )
        return mp4, poster

    def list_tagged_videos(
        self, tag: str, next_cursor: str | None
    ) -> tuple[list[dict], str | None]:
        kwargs: dict = {"resource_type": "video", "max_results": 100}
        if next_cursor:
            kwargs["next_cursor"] = next_cursor
        page = cloudinary.api.resources_by_tag(tag, **kwargs)
        return page.get("resources", []), page.get("next_cursor")
```
Replace `destroy`:
```python
    def destroy(self, public_id: str, *, resource_type: str) -> tuple[bool, str | None]:
        """Delete an asset; resource_type ("image"|"video") is required. 'not found' = success."""
        if not self.enabled:
            return False, "Cloudinary not configured"
        try:
            res = cloudinary.uploader.destroy(
                public_id, resource_type=resource_type, invalidate=True
            )
            if res.get("result") in ("ok", "not found"):
                return True, None
            return False, f"Failed to delete {resource_type}"
        except Exception as e:
            logger.error(f"Cloudinary destroy failed for {public_id}: {type(e).__name__}")
            return False, f"Failed to delete {resource_type}"
```
> If `test_delivery_urls` fails only on the URL-segment order Cloudinary generates (e.g. `c_limit,q_auto,vc_h264,w_1280`), keep the substring assertions and fix the `endswith`. Then confirm on staging (Task 10) that the delivered URL returns 200 **and** is the eager derivative (response header `x-cld-...`/fast first byte). If it isn't, switch `_VIDEO_TRANSFORM` to the raw string form `[{"raw_transformation": "c_limit,q_auto,vc_h264,w_1280"}]`.

Callers:
- `attachment_cleanup.py:47` → `cloudinary_manager.destroy(att.cloudinary_public_id, resource_type=att.kind.value)`
- `account_purge_service.py`: select `(MessageAttachment.cloudinary_public_id, MessageAttachment.kind)` and call `cloudinary_manager.destroy(public_id, resource_type=kind.value)`.
- Tests: `tests/workers/test_attachment_cleanup.py` → `d.assert_called_once_with("chat/old", resource_type="image")` and `def fake_destroy(public_id, *, resource_type)`; apply the same to any purge test that asserts destroy calls. Add one cleanup test: an UPLOADED `kind=VIDEO` orphan older than 24 h is destroyed with `resource_type="video"`.

- [ ] **Step 4: Run**

Run: `poetry run pytest tests/utils tests/workers/test_attachment_cleanup.py tests/workers/test_account_deletion_purge.py -v`
Expected: PASS. Then `grep -rn "\.destroy(" app` should show every `cloudinary_manager.destroy` call passing `resource_type=`. (`cloudinary_client.py:152` is a separate, older image helper using the SDK directly; leave it.)

- [ ] **Step 5: Commit**

```bash
git add app/utils/cloudinary_client.py app/workers/attachment_cleanup.py app/services/account_purge_service.py tests/
git commit -m "feat(cloudinary): video sign/verify/URLs, tag listing; destroy requires resource_type"
```

---

### Task 4: Gate + exposure (widget, hosted page, authed chat, admin settings, platform caps)

**Files:**
- Create: `app/services/screen_recording/__init__.py` (empty), `app/services/screen_recording/gating.py`
- Modify: `app/schemas/widget.py` (`WidgetConfigUpdate` ~:44, `WidgetConfigResponse` ~:63, `PublicWidgetConfigResponse` ~:79), `app/schemas/visitor.py:37`, `app/schemas/customer_conversation.py:38`, `app/schemas/tenant.py` (capabilities response ~:166 and `TenantCapabilitiesUpdate` ~:266)
- Modify: `app/api/v1/endpoints/widget.py` (:75, :132-147, :446), `app/services/visitor_service.py` (:98, :438), `app/api/v1/endpoints/customer_conversations.py:179`
- Test: `tests/services/test_screen_recording_gate.py`

**Interfaces:**
- Produces: `screen_recording_allowed(db: Session, tenant: Tenant | None) -> bool`. Wire fields: public `allow_screen_recording: bool` (default `False` on schemas); admin `widget_allow_screen_recording: bool` + read-only `can_use_screen_recording: bool` on `WidgetConfigResponse`; `widget_allow_screen_recording: bool | None` on `WidgetConfigUpdate`; `can_use_screen_recording` on the capability schemas.

- [ ] **Step 1: Write the failing tests**

```python
"""screen_recording_allowed = capability AND tenant preference; fails closed."""

from unittest.mock import patch

from app.db.models.tenant import Tenant, TenantCapabilities
from app.services.screen_recording.gating import screen_recording_allowed
from tests.api.v1 import test_chat_image_send as img


def _tenant(db, *, cap: bool, pref: bool):
    _, tid = img._session(db)  # seeds TenantCapabilities
    db.query(TenantCapabilities).filter_by(tenant_id=tid).update({"can_use_screen_recording": cap})
    db.query(Tenant).filter_by(id=tid).update({"widget_allow_screen_recording": pref})
    db.commit()
    return db.get(Tenant, tid)


def test_both_on(db):
    assert screen_recording_allowed(db, _tenant(db, cap=True, pref=True)) is True


def test_capability_off(db):
    assert screen_recording_allowed(db, _tenant(db, cap=False, pref=True)) is False


def test_preference_off(db):
    assert screen_recording_allowed(db, _tenant(db, cap=True, pref=False)) is False


def test_missing_capabilities_row_fails_closed(db):
    t = _tenant(db, cap=True, pref=True)
    db.query(TenantCapabilities).filter_by(tenant_id=t.id).delete()
    db.commit()
    assert screen_recording_allowed(db, t) is False


def test_none_tenant_and_exception_fail_closed(db):
    assert screen_recording_allowed(db, None) is False
    t = _tenant(db, cap=True, pref=True)
    with patch.object(db, "execute", side_effect=RuntimeError("boom")):
        assert screen_recording_allowed(db, t) is False


def test_public_widget_config_exposes_flag(client, db):
    from tests.api.v1.test_widget_public_config_first_responder import _get_config

    on = _tenant(db, cap=True, pref=True)
    off = _tenant(db, cap=False, pref=True)
    r_on, r_off = _get_config(client, on.id), _get_config(client, off.id)
    assert r_on.status_code == 200, r_on.text
    assert r_on.json()["allow_screen_recording"] is True
    assert r_off.json()["allow_screen_recording"] is False
```

- [ ] **Step 2: Run to verify failure**

Run: `poetry run pytest tests/services/test_screen_recording_gate.py -v`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement**

`app/services/screen_recording/gating.py`:
```python
"""Screen-recording gate (spec SR-D3): platform capability AND tenant preference.

Fails closed: a missing capabilities row, a missing tenant or any error means off.
"""

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.tenant import Tenant, TenantCapabilities


def screen_recording_allowed(db: Session, tenant: Tenant | None) -> bool:
    try:
        if tenant is None or not tenant.widget_allow_screen_recording:
            return False
        caps = db.execute(
            select(TenantCapabilities).where(TenantCapabilities.tenant_id == tenant.id)
        ).scalar_one_or_none()
        return bool(caps and caps.can_use_screen_recording)
    except Exception:
        logger.exception("screen_recording_allowed failed; failing closed")
        return False
```
Schemas: add `allow_screen_recording: bool = Field(False, description="Customer screen recording available")` to `PublicWidgetConfigResponse`; `allow_screen_recording: bool = False` to `app/schemas/visitor.py` (beside `allow_file_upload`) and `CustomerConversationDetail`; `widget_allow_screen_recording: bool | None = Field(None, description="Allow customer screen recordings")` to `WidgetConfigUpdate`; `widget_allow_screen_recording: bool` and `can_use_screen_recording: bool = True` to `WidgetConfigResponse`; `can_use_screen_recording: bool = Field(default=True, description="Platform grant for customer screen recordings")` to the capabilities response schema and `can_use_screen_recording: bool | None = None` to `TenantCapabilitiesUpdate`.

Endpoints: next to each `allow_file_upload=tenant.widget_allow_file_upload` add `allow_screen_recording=screen_recording_allowed(db, tenant)` (in `visitor_service.py` use `self.db`; in `customer_conversations.py:179` pass `screen_recording_allowed(db, tenant)`, which is False when `tenant` is None). In `widget.py` admin GET/PATCH: handle `config_data.widget_allow_screen_recording` like `widget_allow_file_upload`, and add `widget_allow_screen_recording=tenant.widget_allow_screen_recording` and `can_use_screen_recording=bool(caps and caps.can_use_screen_recording)` (load caps with the same `select(TenantCapabilities)` query) to both `WidgetConfigResponse(...)` constructions. Extract a tiny local helper `_widget_config_response(db, tenant)` to avoid duplicating the 11-field constructor a third time.

- [ ] **Step 4: Run**

Run: `poetry run pytest tests/services/test_screen_recording_gate.py tests/api/v1 -k "widget or visitor or customer_conversation or capabilit" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/screen_recording app/schemas app/api/v1/endpoints/widget.py app/api/v1/endpoints/customer_conversations.py app/services/visitor_service.py tests/services/test_screen_recording_gate.py
git commit -m "feat(screen-recording): capability+preference gate, exposed on public, authed and admin configs"
```

---

### Task 5: `POST /chat/video/sign` and `POST /chat/video/register`

**Files:**
- Modify: `app/utils/http_rate_limiter.py` (beside `upload_rate_limiter` ~:62 and `chat_upload_rate_limit` ~:237)
- Create: `app/api/v1/endpoints/chat_video.py`
- Modify: `app/api/v1/api.py:268` (mount)
- Test: `tests/api/v1/test_chat_video_endpoints.py`

**Interfaces:**
- Consumes: `screen_recording_allowed` (Task 4); `cloudinary_manager.sign_video_upload / video_folder / get_video_resource / video_delivery_urls / destroy` (Task 3); `_upload_response` (Task 2, import from `app.api.v1.endpoints.chat`); Task 1 model fields.
- Produces: `SignVideoRequest{session_id: UUID}`, `RegisterVideoRequest{session_id: UUID, public_id: str}`; sign 200 `{upload_url, fields, max_bytes, max_duration_s}`; register 200 = `_upload_response(row)`; errors as in the table below.

| Case | Status | `detail` |
|---|---|---|
| unknown session | 404 | `"Session not found"` (same as `/chat/upload`) |
| gate off | 403 | `{"code":"screen_recording_disabled","message":"Screen recording is not enabled"}` |
| ≥ 3 videos already in session (sign) | 429 | `{"code":"video_session_limit","message":"Recording limit reached for this conversation"}` |
| Cloudinary not configured (sign) | 503 | `{"code":"video_unavailable","message":"Video upload is not available"}` |
| public_id not in this session's video folder | 422 | `{"code":"video_invalid","message":"We couldn't process that video"}`, **no destroy** |
| already registered | 409 | `{"code":"already_registered","attachment_id":"<id>","message":"Already registered"}` |
| asset missing | 422 | `video_invalid` |
| not video / bad format | 422 | `video_invalid` (asset destroyed) |
| duration > 61 s | 422 | `{"code":"video_too_long","message":"Videos can be up to 60 seconds"}` (destroyed) |
| bytes > 50 MB | 422 | `{"code":"video_too_large","message":"Videos can be up to 50 MB"}` (destroyed) |

- [ ] **Step 1: Write the failing tests**

```python
"""Video sign/register endpoints. The Cloudinary SDK surface is mocked."""

from unittest.mock import patch
from uuid import UUID

from sqlalchemy import select

from app.db.models.conversation import (
    AttachmentKind,
    AttachmentModeration,
    AttachmentStatus,
    MessageAttachment,
)
from app.db.models.tenant import Tenant
from tests.api.v1 import test_chat_image_send as img

CM = "app.api.v1.endpoints.chat_video.cloudinary_manager"


def _seed(db, *, allowed=True):
    sid, tid = img._session(db)
    db.query(Tenant).filter_by(id=tid).update({"widget_allow_screen_recording": allowed})
    db.commit()
    return sid, tid


def _folder(tid, sid):
    return f"luran/chat_images/{tid}/{sid}/video"


def _resource(tid, sid, **kw):
    base = {
        "public_id": f"{_folder(tid, sid)}/abc",
        "resource_type": "video",
        "format": "webm",
        "duration": 42.5,
        "bytes": 5_000_000,
        "width": 1280,
        "height": 720,
        "version": 111,
    }
    return {**base, **kw}


def _cm(cm, tid, sid, resource):
    cm.video_folder.return_value = _folder(tid, sid)
    cm.get_video_resource.return_value = resource
    cm.video_delivery_urls.return_value = ("https://c/v.mp4", "https://c/p.jpg")
    cm.destroy.return_value = (True, None)


def test_sign_ok(client, db):
    sid, tid = _seed(db)
    with patch(CM) as cm:
        cm.sign_video_upload.return_value = {"upload_url": "u", "fields": {"a": 1}, "public_id": "p"}
        r = client.post("/api/v1/chat/video/sign", json={"session_id": str(sid)})
    assert r.status_code == 200, r.text
    assert r.json() == {"upload_url": "u", "fields": {"a": 1}, "max_bytes": 52428800, "max_duration_s": 60}


def test_sign_gate_off_403(client, db):
    sid, _ = _seed(db, allowed=False)
    r = client.post("/api/v1/chat/video/sign", json={"session_id": str(sid)})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "screen_recording_disabled"


def test_sign_unknown_session_404(client, db):
    r = client.post("/api/v1/chat/video/sign", json={"session_id": "00000000-0000-0000-0000-000000000000"})
    assert r.status_code == 404


def test_sign_session_video_cap(client, db):
    sid, tid = _seed(db)
    for _ in range(3):
        aid = img._att(db, sid, tid)
        db.query(MessageAttachment).filter_by(id=aid).update({"kind": AttachmentKind.VIDEO})
    db.commit()
    with patch(CM):
        r = client.post("/api/v1/chat/video/sign", json={"session_id": str(sid)})
    assert r.status_code == 429 and r.json()["detail"]["code"] == "video_session_limit"


def test_register_ok_creates_unlinked_video(client, db):
    sid, tid = _seed(db)
    with patch(CM) as cm:
        _cm(cm, tid, sid, _resource(tid, sid))
        r = client.post(
            "/api/v1/chat/video/register",
            json={"session_id": str(sid), "public_id": f"{_folder(tid, sid)}/abc"},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "video" and body["url"] == "https://c/v.mp4"
    assert body["poster_url"] == "https://c/p.jpg" and body["duration_ms"] == 42500
    row = db.execute(
        select(MessageAttachment).where(MessageAttachment.id == UUID(body["attachment_id"]))
    ).scalar_one()
    assert row.kind == AttachmentKind.VIDEO and row.status == AttachmentStatus.UPLOADED
    assert row.moderation_status == AttachmentModeration.NOT_APPLICABLE
    assert row.content_type == "video/mp4" and row.source_format == "webm"
    assert row.message_id is None


def test_register_foreign_folder_422_without_destroy(client, db):
    sid, tid = _seed(db)
    other_sid, _ = _seed(db)
    with patch(CM) as cm:
        _cm(cm, tid, sid, None)
        r = client.post(
            "/api/v1/chat/video/register",
            json={"session_id": str(sid), "public_id": f"{_folder(tid, other_sid)}/abc"},
        )
    assert r.status_code == 422 and r.json()["detail"]["code"] == "video_invalid"
    cm.destroy.assert_not_called()
    cm.get_video_resource.assert_not_called()


def test_register_too_long_destroys(client, db):
    sid, tid = _seed(db)
    with patch(CM) as cm:
        _cm(cm, tid, sid, _resource(tid, sid, duration=75.0))
        r = client.post(
            "/api/v1/chat/video/register",
            json={"session_id": str(sid), "public_id": f"{_folder(tid, sid)}/abc"},
        )
    assert r.status_code == 422 and r.json()["detail"]["code"] == "video_too_long"
    cm.destroy.assert_called_once_with(f"{_folder(tid, sid)}/abc", resource_type="video")


def test_register_too_large_and_bad_format(client, db):
    sid, tid = _seed(db)
    pid = f"{_folder(tid, sid)}/abc"
    with patch(CM) as cm:
        _cm(cm, tid, sid, _resource(tid, sid, bytes=60_000_000))
        r1 = client.post("/api/v1/chat/video/register", json={"session_id": str(sid), "public_id": pid})
        _cm(cm, tid, sid, _resource(tid, sid, format="avi"))
        r2 = client.post("/api/v1/chat/video/register", json={"session_id": str(sid), "public_id": pid})
    assert r1.json()["detail"]["code"] == "video_too_large"
    assert r2.json()["detail"]["code"] == "video_invalid"


def test_register_twice_409_with_same_id(client, db):
    sid, tid = _seed(db)
    pid = f"{_folder(tid, sid)}/abc"
    with patch(CM) as cm:
        _cm(cm, tid, sid, _resource(tid, sid))
        first = client.post("/api/v1/chat/video/register", json={"session_id": str(sid), "public_id": pid})
        second = client.post("/api/v1/chat/video/register", json={"session_id": str(sid), "public_id": pid})
    assert first.status_code == 200 and second.status_code == 409
    assert second.json()["detail"]["attachment_id"] == first.json()["attachment_id"]
    assert db.query(MessageAttachment).filter_by(cloudinary_public_id=pid).count() == 1


def test_register_missing_asset_422(client, db):
    sid, tid = _seed(db)
    with patch(CM) as cm:
        _cm(cm, tid, sid, None)
        r = client.post(
            "/api/v1/chat/video/register",
            json={"session_id": str(sid), "public_id": f"{_folder(tid, sid)}/abc"},
        )
    assert r.status_code == 422 and r.json()["detail"]["code"] == "video_invalid"
```

- [ ] **Step 2: Run to verify failure**

Run: `poetry run pytest tests/api/v1/test_chat_video_endpoints.py -v`
Expected: FAIL (404 on the routes).

- [ ] **Step 3: Rate limiters** in `app/utils/http_rate_limiter.py`:
```python
video_sign_rate_limiter = RedisRateLimiter(
    key_prefix="http_video", max_calls=5, window_seconds=3600
)
video_register_rate_limiter = RedisRateLimiter(
    key_prefix="http_video_register", max_calls=20, window_seconds=3600
)
...
# Public screen-recording endpoints (separate buckets so videos never consume image uploads).
chat_video_sign_rate_limit = create_rate_limit_dependency(video_sign_rate_limiter, "ip")
chat_video_register_rate_limit = create_rate_limit_dependency(video_register_rate_limiter, "ip")
```
The autouse `_reset_http_rate_limits` fixture (`tests/conftest.py:72`) already clears every `http_*` key, so the new buckets are reset between tests with no change.

- [ ] **Step 4: Endpoints** in `app/api/v1/endpoints/chat_video.py`:
```python
"""Customer screen recordings: signed direct upload + verified registration (spec §4.3)."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.v1.endpoints.chat import _upload_response
from app.core.config import settings
from app.db.models.conversation import (
    AttachmentDirection,
    AttachmentKind,
    AttachmentModeration,
    AttachmentStatus,
    ConversationSession,
    MessageAttachment,
)
from app.db.models.tenant import Tenant
from app.db.session import get_db
from app.services.screen_recording.gating import screen_recording_allowed
from app.utils.cloudinary_client import cloudinary_manager
from app.utils.http_rate_limiter import (
    chat_video_register_rate_limit,
    chat_video_sign_rate_limit,
)

router = APIRouter()

MAX_VIDEOS_PER_SESSION = 3
ALLOWED_VIDEO_FORMATS = {"webm", "mp4", "mov"}
_DURATION_SLACK_S = 1.0


class SignVideoRequest(BaseModel):
    session_id: UUID


class RegisterVideoRequest(BaseModel):
    session_id: UUID
    public_id: str = Field(..., min_length=1, max_length=255)


def _err(code: int, key: str, message: str, **extra) -> HTTPException:
    return HTTPException(code, detail={"code": key, "message": message, **extra})


def _gated_session(db: Session, session_id: UUID) -> ConversationSession:
    session = db.get(ConversationSession, session_id)
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Session not found")
    tenant = db.get(Tenant, session.tenant_id)
    if not screen_recording_allowed(db, tenant):
        raise _err(403, "screen_recording_disabled", "Screen recording is not enabled")
    return session


@router.post("/video/sign", dependencies=[Depends(chat_video_sign_rate_limit)])
def sign_video(body: SignVideoRequest, db: Session = Depends(get_db)) -> dict:
    session = _gated_session(db, body.session_id)
    used = db.execute(
        select(func.count())
        .select_from(MessageAttachment)
        .where(
            MessageAttachment.session_id == session.id,
            MessageAttachment.kind == AttachmentKind.VIDEO,
        )
    ).scalar_one()
    if used >= MAX_VIDEOS_PER_SESSION:
        raise _err(429, "video_session_limit", "Recording limit reached for this conversation")
    signed = cloudinary_manager.sign_video_upload(session.tenant_id, session.id)
    if signed is None:
        raise _err(503, "video_unavailable", "Video upload is not available")
    return {
        "upload_url": signed["upload_url"],
        "fields": signed["fields"],
        "max_bytes": settings.SCREEN_RECORDING_MAX_BYTES,
        "max_duration_s": settings.SCREEN_RECORDING_MAX_SECONDS,
    }


@router.post("/video/register", dependencies=[Depends(chat_video_register_rate_limit)])
def register_video(body: RegisterVideoRequest, db: Session = Depends(get_db)) -> dict:
    session = _gated_session(db, body.session_id)
    folder = cloudinary_manager.video_folder(session.tenant_id, session.id)
    # Ownership first: never look up or destroy an asset outside this session's folder.
    if not body.public_id.startswith(folder + "/") or ".." in body.public_id:
        raise _err(422, "video_invalid", "We couldn't process that video")

    existing = db.execute(
        select(MessageAttachment).where(
            MessageAttachment.cloudinary_public_id == body.public_id,
            MessageAttachment.kind == AttachmentKind.VIDEO,
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise _err(409, "already_registered", "Already registered", attachment_id=str(existing.id))

    res = cloudinary_manager.get_video_resource(body.public_id)
    if res is None:
        raise _err(422, "video_invalid", "We couldn't process that video")

    def _reject(key: str, message: str) -> HTTPException:
        ok, _ = cloudinary_manager.destroy(body.public_id, resource_type="video")
        if not ok:
            logger.warning("Rejected video destroy failed; orphan sweep will retry")
        return _err(422, key, message)

    if res.get("resource_type") != "video" or res.get("format") not in ALLOWED_VIDEO_FORMATS:
        raise _reject("video_invalid", "We couldn't process that video")
    duration = float(res.get("duration") or 0)
    if duration <= 0 or duration > settings.SCREEN_RECORDING_MAX_SECONDS + _DURATION_SLACK_S:
        raise _reject("video_too_long", "Videos can be up to 60 seconds")
    size = int(res.get("bytes") or 0)
    if size <= 0 or size > settings.SCREEN_RECORDING_MAX_BYTES:
        raise _reject("video_too_large", "Videos can be up to 50 MB")

    mp4_url, poster_url = cloudinary_manager.video_delivery_urls(body.public_id, res.get("version"))
    row = MessageAttachment(
        session_id=session.id,
        tenant_id=session.tenant_id,
        direction=AttachmentDirection.INBOUND,
        status=AttachmentStatus.UPLOADED,
        moderation_status=AttachmentModeration.NOT_APPLICABLE,
        kind=AttachmentKind.VIDEO,
        cloudinary_public_id=body.public_id,
        url=mp4_url,
        poster_url=poster_url,
        content_type="video/mp4",
        source_format=res.get("format"),
        duration_ms=int(round(duration * 1000)),
        width=res.get("width"),
        height=res.get("height"),
        bytes=size,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _upload_response(row)
```
> A zero-duration asset is reported as `video_too_long`. That's acceptable: the customer copy is the same "up to 60 s" message, and Cloudinary reports a positive duration for every real clip. A concurrent double-register can race past the `existing` check. The partial unique index then raises `IntegrityError` on commit; catch it, `db.rollback()`, re-select, and raise the same 409.

Mount in `app/api/v1/api.py`, directly under line 268 (`api_router.include_router(chat.router, prefix="/chat", tags=["chat"])`): `api_router.include_router(chat_video.router, prefix="/chat", tags=["chat"])`, and add `chat_video` to that module's endpoint imports.

- [ ] **Step 5: Run**

Run: `poetry run pytest tests/api/v1/test_chat_video_endpoints.py tests/api/v1/test_chat_image_upload.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/api/v1/endpoints/chat_video.py app/api/v1/api.py app/utils/http_rate_limiter.py tests/api/v1/test_chat_video_endpoints.py
git commit -m "feat(screen-recording): signed direct upload + Admin-API-verified registration endpoints"
```

---

### Task 6: Send path — one-video rule, escalation turn, AI exclusion

**Files:**
- Modify: `app/services/first_responder.py` (constants)
- Modify: `app/api/v1/endpoints/chat.py`: attachment validation (~:1214-1222), insert the video branch before the escalated early return (~:1275), and `_turn_image_urls` (~:2555)
- Test: `tests/api/v1/test_chat_video_send.py`

**Interfaces:**
- Consumes: `_escalate_to_human`, `_get_missing_contact_fields`, `_store_pending_contact_capture`, `_build_contact_capture_prompt` (all existing in `chat.py`); Task 1 `AttachmentKind`; Task 2 `AttachmentOut.kind`.
- Produces: `SCREEN_RECORDING_ACK`, `SCREEN_RECORDING_REASON`, `VIDEO_ONLY_PLACEHOLDER = "[screen recording]"` (in `chat.py`). Response for a fresh video turn: `ChatResponse(message=<ACK assistant msg, meta.system_notice="screen_recording_ack">, should_escalate=True, escalation_reason=SCREEN_RECORDING_REASON, ticket_id=<id>)`, plus `user_message_attachments` filled by the existing `send_message` wrapper.

- [ ] **Step 1: Write the failing tests**

```python
"""POST /chat/message with a video attachment escalates and never reaches the AI."""

from unittest.mock import AsyncMock, patch

from sqlalchemy import select

from app.db.models.conversation import (
    AttachmentKind,
    ConversationSession,
    Message,
    MessageAttachment,
    SessionStatus,
    Ticket,
    TicketStatus,
)
from app.db.models.tenant import TenantAIConfig
from app.services.first_responder import SCREEN_RECORDING_ACK, SCREEN_RECORDING_REASON
from tests.api.v1 import test_chat_image_send as img

RING = "app.api.v1.endpoints.chat._ring_agents"
EMAILS = "app.api.v1.endpoints.chat._queue_escalation_emails"


def _video(db, sid, tid):
    aid = img._att(db, sid, tid)
    db.query(MessageAttachment).filter_by(id=aid).update(
        {"kind": AttachmentKind.VIDEO, "content_type": "video/mp4", "url": "https://c/v.mp4",
         "cloudinary_public_id": f"vid/{aid}", "duration_ms": 5000}
    )
    db.commit()
    return aid


def _send(client, sid, message, ids):
    with patch(RING, new=AsyncMock()) as ring, patch(EMAILS):
        r = img._send(client, sid, message, [str(i) for i in ids])
    return r, ring


def test_video_escalates_with_ack_and_no_ai(client, db):
    sid, tid = img._session(db)
    vid = _video(db, sid, tid)
    r, ring = _send(client, sid, "", [vid])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["message"]["content"] == SCREEN_RECORDING_ACK
    assert body["should_escalate"] is True and body["escalation_reason"] == SCREEN_RECORDING_REASON
    assert body["ticket_id"]
    assert body["user_message_attachments"][0]["kind"] == "video"
    assert img._SEEN_IMAGE_URLS == []  # generation never ran
    ring.assert_awaited_once()
    s = db.get(ConversationSession, sid)
    db.refresh(s)
    assert s.status == SessionStatus.ESCALATED
    row = db.get(MessageAttachment, vid)
    db.refresh(row)
    user_msg = db.get(Message, row.message_id)
    assert user_msg.content == "[screen recording]"


def test_video_with_text_keeps_text(client, db):
    sid, tid = img._session(db)
    vid = _video(db, sid, tid)
    r, _ = _send(client, sid, "the button does nothing", [vid])
    row = db.get(MessageAttachment, vid)
    db.refresh(row)
    assert db.get(Message, row.message_id).content == "the button does nothing"


def test_two_videos_rejected(client, db):
    sid, tid = img._session(db)
    a, b = _video(db, sid, tid), _video(db, sid, tid)
    r, _ = _send(client, sid, "", [a, b])
    assert r.status_code == 422 and r.json()["detail"]["code"] == "too_many_videos"
    assert db.get(MessageAttachment, a).message_id is None


def test_video_plus_image_allowed_and_image_not_sent_to_ai(client, db):
    sid, tid = img._session(db)
    vid, pic = _video(db, sid, tid), img._att(db, sid, tid)
    r, _ = _send(client, sid, "", [vid, pic])
    assert r.status_code == 200 and r.json()["message"]["content"] == SCREEN_RECORDING_ACK
    assert img._SEEN_IMAGE_URLS == []


def test_contact_capture_required_persists_video_then_prompts(client, db):
    sid, tid = img._session(db)
    db.query(TenantAIConfig).filter_by(tenant_id=tid).update({"require_email_before_escalation": True})
    db.commit()
    vid = _video(db, sid, tid)
    r, ring = _send(client, sid, "", [vid])
    assert r.status_code == 200
    assert r.json()["should_escalate"] is False
    assert r.json()["message"]["meta"]["contact_capture_required"] is True
    ring.assert_not_awaited()
    assert db.get(MessageAttachment, vid).message_id is not None
    s = db.get(ConversationSession, sid)
    db.refresh(s)
    assert s.meta["contact_capture_context"]["reason"] == SCREEN_RECORDING_REASON


def test_already_escalated_in_progress_no_ack_no_ring(client, db):
    sid, tid = img._session(db)
    first = _video(db, sid, tid)
    _send(client, sid, "", [first])
    t = db.execute(select(Ticket).where(Ticket.session_id == sid)).scalar_one()
    t.status = TicketStatus.IN_PROGRESS
    db.commit()
    second = _video(db, sid, tid)
    r, ring = _send(client, sid, "", [second])
    assert r.status_code == 200
    assert r.json()["message"]["role"] == "user"  # echo, no ACK
    ring.assert_not_awaited()
    assert db.execute(select(Ticket).where(Ticket.session_id == sid)).scalars().all().__len__() == 1


def test_already_escalated_missed_rerings(client, db):
    sid, tid = img._session(db)
    _send(client, sid, "", [_video(db, sid, tid)])
    t = db.execute(select(Ticket).where(Ticket.session_id == sid)).scalar_one()
    t.status = TicketStatus.MISSED
    t.assigned_to = None
    db.commit()
    r, ring = _send(client, sid, "", [_video(db, sid, tid)])
    assert r.status_code == 200
    ring.assert_awaited_once()


def test_human_first_tenant_gets_video_ack(client, db):
    sid, tid = img._session(db)
    db.query(TenantAIConfig).filter_by(tenant_id=tid).update({"first_responder": "human"})
    db.commit()
    r, _ = _send(client, sid, "", [_video(db, sid, tid)])
    assert r.json()["message"]["content"] == SCREEN_RECORDING_ACK
```
> The per-session cap (3) is enforced at sign time (Task 5); these tests seed rows directly, so only the send path's rules are under test.

- [ ] **Step 2: Run to verify failure**

Run: `poetry run pytest tests/api/v1/test_chat_video_send.py -v`
Expected: FAIL (`ImportError: SCREEN_RECORDING_ACK`).

- [ ] **Step 3: Constants** in `app/services/first_responder.py`:
```python
SCREEN_RECORDING_REASON = "Customer sent a screen recording"
SCREEN_RECORDING_ACK = "Thanks — a member of our team will watch your recording and reply here."
```
In `chat.py` import both from `app.services.first_responder` (extend the existing import block at ~:80), and add beside `IMAGE_ONLY_PLACEHOLDER`: `VIDEO_ONLY_PLACEHOLDER = "[screen recording]"`.

- [ ] **Step 4: One-video rule.** Replace the up-front validation (~:1214-1222):
```python
    turn_has_video = False
    if message_data.attachment_ids:
        try:
            _linkable = conversation_service.get_linkable_attachments(
                session_id, message_data.attachment_ids
            )
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid or already-used attachment",
            ) from None
        _video_count = sum(1 for a in _linkable if a.kind == AttachmentKind.VIDEO)
        if _video_count > 1:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"code": "too_many_videos", "message": "One recording per message"},
            )
        turn_has_video = _video_count == 1
```
(Keep the rest of that block, from `_attachment_ids = ...` and the `add_message` wrapper, unchanged.)

- [ ] **Step 5: Video turn branch.** Insert immediately **before** the comment block that precedes `if session.status == SessionStatus.ESCALATED and effective_message_lower != TALK_TO_HUMAN_TOKEN:` (~:1264):
```python
    # Screen recording (spec SR-D4): a video is a request for a human. It never reaches the
    # AI, flows or automations. Same contact gate + _escalate_to_human as talk-to-human;
    # an already-escalated session only re-rings when its ticket is MISSED (inside the helper).
    if turn_has_video:
        video_text = (
            VIDEO_ONLY_PLACEHOLDER
            if message_data.message == IMAGE_ONLY_PLACEHOLDER
            else message_data.message
        )
        video_user_message = conversation_service.add_message(
            session_id=session_id, role=MessageRole.USER, content=video_text
        )
        already_escalated = session.status == SessionStatus.ESCALATED
        if not already_escalated:
            video_missing = _get_missing_contact_fields(
                require_name_before_escalation=require_name_before_escalation,
                require_email_before_escalation=require_email_before_escalation,
                customer_name=customer_name,
                customer_contact_email=customer_contact_email,
            )
            if video_missing:
                _store_pending_contact_capture(
                    conversation_service=conversation_service,
                    session_id=session_id,
                    session_meta=dict(session_model.meta or {}),
                    escalation_reason=SCREEN_RECORDING_REASON,
                    customer_query=video_text,
                    required_fields=video_missing,
                )
                video_prompt = conversation_service.add_message(
                    session_id=session_id,
                    role=MessageRole.ASSISTANT,
                    content=_build_contact_capture_prompt(video_missing),
                    meta={
                        "contact_capture_required": True,
                        "contact_capture_fields": video_missing,
                    },
                )
                return ChatResponse(
                    session_id=session_id,
                    message=video_prompt,
                    should_escalate=False,
                    escalation_reason=None,
                    ticket_id=None,
                )
        video_ticket_id = await _escalate_to_human(
            db,
            conversation_service=conversation_service,
            tenant_service=tenant_service,
            session=session,
            session_id=session_id,
            escalation_reason=SCREEN_RECORDING_REASON,
            customer_query=video_text,
            conversation_history=conversation_service.get_session_messages(
                session_id=session_id, limit=20
            ),
            contact_email=customer_contact_email,
        )
        if already_escalated:
            return ChatResponse(
                session_id=session_id,
                message=video_user_message,
                should_escalate=False,
                escalation_reason=None,
                ticket_id=None,
            )
        video_ack = conversation_service.add_message(
            session_id=session_id,
            role=MessageRole.ASSISTANT,
            content=SCREEN_RECORDING_ACK,
            meta={"system_notice": "screen_recording_ack"},
        )
        return ChatResponse(
            session_id=session_id,
            message=video_ack,
            should_escalate=True,
            escalation_reason=SCREEN_RECORDING_REASON,
            ticket_id=video_ticket_id,
        )
```
> `customer_name`, `customer_contact_email`, `require_*_before_escalation`, `session_model`, `tenant_service` are all bound earlier in `_send_message_impl` (~:1171-1210). Confirm the names when editing. When the customer later supplies contact details, the existing pending-contact completion path escalates with the stored `SCREEN_RECORDING_REASON`. The ring payload already carries `session.escalation_reason`, so agents see "Customer sent a screen recording" with no alert-worker change.

- [ ] **Step 6: AI exclusion (belt and braces).** At ~:2555:
```python
            _turn_image_urls = [
                a.url for a in linked_attachments if a.kind == "image" and a.url
            ] or None
```

- [ ] **Step 7: Run the new tests and the regression suites**

Run: `poetry run pytest tests/api/v1/test_chat_video_send.py tests/api/v1/test_chat_image_send.py tests/api/v1/test_chat_flow_router.py tests/api/v1/test_customer_conversations.py -v`
Expected: PASS. If `test_contact_capture_required…` fails because `TenantAIConfig` has no `require_email_before_escalation` column under that name, use the attribute read at `chat.py` ~:1172 instead.

- [ ] **Step 8: Commit**

```bash
git add app/services/first_responder.py app/api/v1/endpoints/chat.py tests/api/v1/test_chat_video_send.py
git commit -m "feat(screen-recording): video turns escalate to a human with a fixed ACK and never reach the AI"
```

---

### Task 7: Retention job

**Files:**
- Create: `app/workers/attachment_retention.py`
- Modify: `app/workers/document_processor.py` (cron list ~:1303 and the imports)
- Test: `tests/workers/test_attachment_retention.py`

**Interfaces:**
- Consumes: `cloudinary_manager.destroy(public_id, resource_type=)` (Task 3); Task 1 fields; `Ticket`, `TicketStatus`.
- Produces: `async def expire_attachments(ctx: dict) -> int` (rows expired); constants `BATCH_SIZE = 200`, `MAX_BATCHES = 10`, `ACTIVE_GRACE = timedelta(days=7)`.

- [ ] **Step 1: Write the failing tests**

```python
"""Daily attachment-retention job."""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from uuid import uuid4

from app.db.models.conversation import (
    AttachmentKind,
    AttachmentStatus,
    MessageAttachment,
    MessageRole,
    Ticket,
    TicketPriority,
    TicketStatus,
)
from app.services.conversation_service import ConversationService
from tests.workers.test_attachment_cleanup import _NoCloseSession, _att, _session

DESTROY = "app.workers.attachment_retention.cloudinary_manager.destroy"


def _linked(db, s, pid, *, expires_at, kind=AttachmentKind.IMAGE):
    msg = ConversationService(db).add_message(s.id, MessageRole.USER, "hi")
    return _att(
        db, s, pid, status=AttachmentStatus.LINKED, message_id=msg.id,
        expires_at=expires_at, kind=kind,
    )


def _ticket(db, s, status):
    t = Ticket(
        tenant_id=s.tenant_id, session_id=s.id, ticket_number=f"T-{uuid4().hex[:6]}",
        priority=TicketPriority.MEDIUM, status=status, summary="x",
    )
    db.add(t)
    db.commit()


async def _run(db, monkeypatch, **destroy_kw):
    import app.workers.attachment_retention as mod

    monkeypatch.setattr(mod, "SessionLocal", lambda: _NoCloseSession(db))
    with patch(DESTROY, **destroy_kw) as d:
        n = await mod.expire_attachments({})
    return n, d


async def test_expires_due_rows_keeps_row_nulls_urls(db, monkeypatch):
    s = _session(db)
    now = datetime.now(UTC)
    due = _linked(db, s, "a/due", expires_at=now - timedelta(hours=1), kind=AttachmentKind.VIDEO)
    future = _linked(db, s, "a/future", expires_at=now + timedelta(days=1))
    due_id, future_id = due.id, future.id
    n, d = await _run(db, monkeypatch, return_value=(True, None))
    assert n == 1
    d.assert_called_once_with("a/due", resource_type="video")
    db.expire_all()
    row = db.get(MessageAttachment, due_id)
    assert row.status == AttachmentStatus.EXPIRED and row.url is None and row.poster_url is None
    assert db.get(MessageAttachment, future_id).status == AttachmentStatus.LINKED


async def test_active_ticket_defers_until_ceiling(db, monkeypatch):
    s = _session(db)
    _ticket(db, s, TicketStatus.IN_PROGRESS)
    now = datetime.now(UTC)
    recent = _linked(db, s, "a/recent", expires_at=now - timedelta(days=1))
    stale = _linked(db, s, "a/stale", expires_at=now - timedelta(days=8))
    recent_id, stale_id = recent.id, stale.id
    n, d = await _run(db, monkeypatch, return_value=(True, None))
    assert n == 1
    d.assert_called_once_with("a/stale", resource_type="image")
    db.expire_all()
    assert db.get(MessageAttachment, recent_id).status == AttachmentStatus.LINKED
    assert db.get(MessageAttachment, stale_id).status == AttachmentStatus.EXPIRED


async def test_resolved_ticket_does_not_defer(db, monkeypatch):
    s = _session(db)
    _ticket(db, s, TicketStatus.RESOLVED)
    _linked(db, s, "a/x", expires_at=datetime.now(UTC) - timedelta(hours=1))
    n, _ = await _run(db, monkeypatch, return_value=(True, None))
    assert n == 1


async def test_failing_destroy_is_isolated_and_terminates(db, monkeypatch):
    s = _session(db)
    past = datetime.now(UTC) - timedelta(hours=1)
    bad = _linked(db, s, "a/bad", expires_at=past)
    good = _linked(db, s, "a/good", expires_at=past)
    bad_id, good_id = bad.id, good.id

    def fake(public_id, *, resource_type):
        return (False, "boom") if public_id == "a/bad" else (True, None)

    n, d = await _run(db, monkeypatch, side_effect=fake)
    assert n == 1
    assert d.call_count == 2  # bad tried once, not retried in a loop
    db.expire_all()
    assert db.get(MessageAttachment, bad_id).status == AttachmentStatus.LINKED
    assert db.get(MessageAttachment, good_id).status == AttachmentStatus.EXPIRED


async def test_kill_switch(db, monkeypatch):
    import app.workers.attachment_retention as mod

    monkeypatch.setattr(mod.settings, "ATTACHMENT_RETENTION_ENABLED", False)
    s = _session(db)
    _linked(db, s, "a/x", expires_at=datetime.now(UTC) - timedelta(hours=1))
    n, d = await _run(db, monkeypatch, return_value=(True, None))
    assert n == 0
    d.assert_not_called()


async def test_uploaded_rows_ignored(db, monkeypatch):
    s = _session(db)
    _att(db, s, "a/up", status=AttachmentStatus.UPLOADED,
         expires_at=datetime.now(UTC) - timedelta(hours=1))
    n, d = await _run(db, monkeypatch, return_value=(True, None))
    assert n == 0  # unsent uploads belong to cleanup_orphan_attachments
```
> `Ticket`'s NOT NULL columns are `tenant_id, session_id, priority, status, summary` (all set above). The existing `_att` helper in `test_attachment_cleanup.py` takes `**kw`, so `expires_at`/`kind` pass through.

- [ ] **Step 2: Run to verify failure**

Run: `poetry run pytest tests/workers/test_attachment_retention.py -v`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement** `app/workers/attachment_retention.py`:
```python
"""Daily chat-attachment retention (spec SR-D5, §4.5).

LINKED attachments past expires_at have their Cloudinary asset destroyed. The row is
kept as EXPIRED with url/poster_url NULL, so transcripts show a placeholder. An
attachment whose session has a live ticket (open / in_progress / missed) is deferred,
but never for more than ACTIVE_GRACE past expiry. One failing row never aborts the run
and is not retried within the same run.
"""

from datetime import timedelta
from uuid import UUID

from loguru import logger
from sqlalchemy import and_, exists, not_, or_, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models.conversation import (
    AttachmentStatus,
    MessageAttachment,
    Ticket,
    TicketStatus,
)
from app.db.session import SessionLocal
from app.utils.cloudinary_client import cloudinary_manager
from app.utils.datetime import utcnow_aware

BATCH_SIZE = 200
MAX_BATCHES = 10
ACTIVE_GRACE = timedelta(days=7)
_LIVE_TICKET = (TicketStatus.OPEN, TicketStatus.IN_PROGRESS, TicketStatus.MISSED)


def _due_query(now, skip: set[UUID]):
    live_ticket = exists().where(
        and_(
            Ticket.session_id == MessageAttachment.session_id,
            Ticket.status.in_(_LIVE_TICKET),
        )
    )
    stmt = (
        select(MessageAttachment)
        .where(
            MessageAttachment.status == AttachmentStatus.LINKED,
            MessageAttachment.expires_at < now,
            or_(not_(live_ticket), MessageAttachment.expires_at < now - ACTIVE_GRACE),
        )
        .order_by(MessageAttachment.expires_at)
        .limit(BATCH_SIZE)
    )
    if skip:
        stmt = stmt.where(MessageAttachment.id.notin_(skip))
    return stmt


async def expire_attachments(ctx: dict) -> int:
    if not settings.ATTACHMENT_RETENTION_ENABLED:
        logger.info("Attachment retention disabled; skipping")
        return 0
    db: Session = SessionLocal()
    expired = 0
    failed: set[UUID] = set()
    try:
        now = utcnow_aware()
        for _ in range(MAX_BATCHES):
            rows = db.execute(_due_query(now, failed)).scalars().all()
            if not rows:
                break
            for att in rows:
                kind = getattr(att.kind, "value", att.kind)
                try:
                    ok, err = cloudinary_manager.destroy(
                        att.cloudinary_public_id, resource_type=kind
                    )
                except Exception as e:  # defensive: destroy returns (ok, err) today
                    ok, err = False, type(e).__name__
                if not ok:
                    logger.warning(
                        "Retention destroy failed for public_id={}: {}; retry next run",
                        att.cloudinary_public_id,
                        err,
                    )
                    failed.add(att.id)
                    continue
                att.status = AttachmentStatus.EXPIRED
                att.url = None
                att.poster_url = None
                db.commit()
                expired += 1
        logger.info("Attachment retention: {} expired, {} deferred to next run", expired, len(failed))
        return expired
    except Exception as e:
        logger.error("Error in expire_attachments: {}", type(e).__name__, exc_info=True)
        db.rollback()
        return expired
    finally:
        db.close()
```
Cron in `document_processor.py` (import `expire_attachments` beside `cleanup_orphan_attachments`, and add it to the worker `functions` list if crons there must also be registered as functions; mirror how `cleanup_orphan_attachments` is registered):
```python
        # Expire chat attachments past their 30-day retention - daily at 3:15 AM UTC
        cron(
            expire_attachments,
            hour={3},
            minute=15,
            run_at_startup=False,
        ),
```

- [ ] **Step 4: Run**

Run: `poetry run pytest tests/workers/test_attachment_retention.py tests/workers/test_attachment_cleanup.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/workers/attachment_retention.py app/workers/document_processor.py tests/workers/test_attachment_retention.py
git commit -m "feat(retention): daily job expires chat attachments 30 days after upload"
```

---

### Task 8: Sweep never-registered video uploads

**Files:**
- Modify: `app/workers/attachment_retention.py` (add `sweep_unregistered_videos`)
- Modify: `app/workers/document_processor.py` (cron 3:30 AM UTC)
- Test: append to `tests/workers/test_attachment_retention.py`

**Interfaces:**
- Consumes: `cloudinary_manager.list_tagged_videos(tag, next_cursor)`, `destroy(..., resource_type="video")` (Task 3).
- Produces: `async def sweep_unregistered_videos(ctx: dict) -> int` (assets destroyed); `SWEEP_MIN_AGE = timedelta(hours=24)`, `SWEEP_MAX_PAGES = 5`, `SWEEP_TAG = "screen_recording"`.

- [ ] **Step 1: Write the failing tests** (append):
```python
LIST = "app.workers.attachment_retention.cloudinary_manager.list_tagged_videos"


async def test_sweep_destroys_old_unregistered_only(db, monkeypatch):
    import app.workers.attachment_retention as mod

    monkeypatch.setattr(mod, "SessionLocal", lambda: _NoCloseSession(db))
    s = _session(db)
    _att(db, s, "v/registered", status=AttachmentStatus.UPLOADED, kind=AttachmentKind.VIDEO)
    old = (datetime.now(UTC) - timedelta(hours=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    new = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    pages = [
        ([{"public_id": "v/registered", "created_at": old},
          {"public_id": "v/orphan", "created_at": old}], "c2"),
        ([{"public_id": "v/fresh", "created_at": new}], None),
    ]
    with patch(LIST, side_effect=pages) as lst, patch(DESTROY, return_value=(True, None)) as d:
        n = await mod.sweep_unregistered_videos({})
    assert n == 1
    d.assert_called_once_with("v/orphan", resource_type="video")
    assert lst.call_count == 2


async def test_sweep_stops_at_page_cap(db, monkeypatch):
    import app.workers.attachment_retention as mod

    monkeypatch.setattr(mod, "SessionLocal", lambda: _NoCloseSession(db))
    with patch(LIST, return_value=([], "again")) as lst, patch(DESTROY):
        await mod.sweep_unregistered_videos({})
    assert lst.call_count == mod.SWEEP_MAX_PAGES
```

- [ ] **Step 2: Run to verify failure**

Run: `poetry run pytest tests/workers/test_attachment_retention.py -k sweep -v`
Expected: FAIL (`sweep_unregistered_videos` missing).

- [ ] **Step 3: Implement** (append to `attachment_retention.py`; add `from datetime import datetime` to imports):
```python
SWEEP_TAG = "screen_recording"
SWEEP_MIN_AGE = timedelta(hours=24)
SWEEP_MAX_PAGES = 5


def _parse_created(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None


async def sweep_unregistered_videos(ctx: dict) -> int:
    """Destroy screen-recording uploads older than 24 h that never got a DB row
    (customer closed the tab between upload and register)."""
    db: Session = SessionLocal()
    destroyed = 0
    try:
        cutoff = utcnow_aware() - SWEEP_MIN_AGE
        cursor: str | None = None
        for _ in range(SWEEP_MAX_PAGES):
            resources, cursor = cloudinary_manager.list_tagged_videos(SWEEP_TAG, cursor)
            old_ids = [
                r["public_id"]
                for r in resources
                if (created := _parse_created(r.get("created_at", ""))) and created < cutoff
            ]
            if old_ids:
                known = set(
                    db.execute(
                        select(MessageAttachment.cloudinary_public_id).where(
                            MessageAttachment.cloudinary_public_id.in_(old_ids)
                        )
                    ).scalars()
                )
                for public_id in old_ids:
                    if public_id in known:
                        continue
                    ok, err = cloudinary_manager.destroy(public_id, resource_type="video")
                    if ok:
                        destroyed += 1
                    else:
                        logger.warning("Sweep destroy failed for public_id={}: {}", public_id, err)
            if not cursor:
                break
        logger.info("Unregistered video sweep: {} destroyed", destroyed)
        return destroyed
    except Exception as e:
        logger.error("Error in sweep_unregistered_videos: {}", type(e).__name__, exc_info=True)
        return destroyed
    finally:
        db.close()
```
Cron: `cron(sweep_unregistered_videos, hour={3}, minute=30, run_at_startup=False)`.

- [ ] **Step 4: Run**

Run: `poetry run pytest tests/workers/test_attachment_retention.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/workers/attachment_retention.py app/workers/document_processor.py tests/workers/test_attachment_retention.py
git commit -m "feat(screen-recording): daily sweep destroys uploaded-but-never-registered videos"
```

---

### Task 9: Reproduce CI locally, review, PR

**Files:** none new (fixes only).

- [ ] **Step 1: Lint/format with the pinned toolchain**

```bash
poetry run ruff --version   # expect 0.8.6
poetry run black --version  # expect 24.10.0
poetry run ruff check app/
poetry run black --check app/
```
Expected: both clean. Fix and re-run until they are.

- [ ] **Step 2: Full suite against throwaway Postgres 15 + Redis 7**

```bash
docker run -d --rm --name sr-ci-pg -e POSTGRES_USER=test_user -e POSTGRES_PASSWORD=test_password -e POSTGRES_DB=test_db -p 55432:5432 postgres:15-alpine
docker run -d --rm --name sr-ci-redis -p 56379:6379 redis:7-alpine
```
Load the test job's `env:` block from `.github/workflows/ci.yml` programmatically (e.g. a small `python -c` that reads the YAML and execs pytest with those env vars), rewrite the ports to 55432/56379, and **never echo the values**. Then run `poetry run pytest tests/ -q`. If Redis 429 flakes appear between full runs, `docker exec sr-ci-redis redis-cli FLUSHDB` and re-run. Also run the focused `automation-flow-regression` job's pytest list from `ci.yml` with `DATABASE_URL=sqlite:///:memory:`.
Expected: all green. Stop the containers afterwards.

- [ ] **Step 3: Reviews.** Dispatch an Opus review of `chat_video.py` (upload trust: folder ownership, destroy-only-own, idempotency, limits) and `attachment_retention.py` + `account_purge_service.py` (data deletion), and a Sonnet whole-branch review. Fix findings, re-run Steps 1–2.

- [ ] **Step 4: Verify no attribution, push, open PR**

```bash
git log origin/develop..HEAD --format=%B | grep -iE "co-authored|claude|generated with" && echo "FOUND - fix before push" || echo clean
git push -u origin feat/screen-recording-be
gh pr create --base develop --title "Screen recording (video attachments) + 30-day attachment retention — backend" --body-file /tmp/pr-body.md
```
PR body: summary, spec link, decision IDs, migration notes (sr10, enum values, image backfill date), rollout flags (`ATTACHMENT_RETENTION_ENABLED`), test evidence. **No attribution footer.** Merge only with Adebayo's go (merge commit). CD deploys staging from `develop` (~10–14 min); then check `/health` and `ssh luran 'docker logs --since 15m luran_staging_api'` for the migration.

---

### Task 10: Live capture on staging → FE guide, SOPs, checklist

**Files:**
- Create: `docs/fe-integration-guide-screen-recording.md`, `docs/sop/screen_recording_backend.md`, `docs/sop/attachment_retention.md`, `docs/captures/screen-recording/*.json`
- Modify: `docs/fe-integration-guide-chat-image-upload.md` (new `kind`, `duration_ms`, `poster_url`, `expired`, nullable `url`)
- Modify: workspace `docs/checklist/luran-master-checklist.md`

- [ ] **Step 1.0: Verify the sr10 backfill on staging data** (read-only): `ssh luran` then the read-only psql pattern from the handoff: `SELECT kind, count(*), min(expires_at), max(expires_at) FROM message_attachments GROUP BY kind;`. Expect every pre-existing row `IMAGE` with `expires_at` ≈ deploy time + 30 d.

- [ ] **Step 1: Capture every shape from staging** with a throwaway staging tenant/session (placeholders in docs). Save each request/response as a JSON file under `docs/captures/screen-recording/`:
  1. `sign` 200, 403 (toggle off via the admin widget PATCH), 404, 429 (4th video in a session)
  2. A real Cloudinary upload with the signed fields (a small test `.webm`/`.mp4` ≤ 10 s), then `register` 200
  3. `register` repeated → 409; foreign folder → 422 `video_invalid`; a >60 s file → 422 `video_too_long`
  4. `/chat/message` with the video → 200 (ACK, `user_message_attachments[0].kind == "video"`); a two-video send → 422 `too_many_videos`
  5. The agent WS `customer_message` frame carrying the video (via an authed agent socket if Adebayo provides a session; otherwise mark it **resolved-from-code** with the `attachment_frames` source line)
  6. An expired attachment: on staging set one row's `expires_at` in the past through a read-write psql session **with Adebayo's OK**, run `expire_attachments` once in the worker container, then capture the REST message with `expired: true, url: null`
  7. `GET` public widget config showing `allow_screen_recording`
  Redact tokens and IDs to obviously-fake placeholders; never paste secret values.

- [ ] **Step 2: Write the guide** from the captures only: what the feature is (FE terms), each endpoint (request, response, status, auth = public by `session_id`), every error code with exact strings, field-nesting traps (`attachment_id` on register vs `id` everywhere else; `url` null when `expired`; `kind` drives rendering, not `content_type`; the ACK is an assistant message with `meta.system_notice`), the client flow (sign → multipart POST of `fields` + `file` to `upload_url` → register → send), the limits, UX consequences (escalation, "kept for 30 days" consent copy, expired placeholders), and a verification table (verified-live / resolved-from-code / pending-human for the real `getDisplayMedia` picker and the iPhone `.mov`).

- [ ] **Step 3: SOPs.**
  - `screen_recording_backend.md`: what, why, how, files, verification, operate/rollback (gate via `can_use_screen_recording` override or tenant toggle; downgrade notes).
  - `attachment_retention.md`: job schedule, kill switch, the 7-day ceiling. **Prod enable procedure:** if retention is enabled in prod more than ~3 days after `sr10` ran, first re-stamp the image grace with `UPDATE message_attachments SET expires_at = now() + interval '30 days' WHERE kind = 'IMAGE' AND status = 'LINKED' AND expires_at < now() + interval '30 days' AND created_at < '<sr10 deploy time>'` (Adebayo runs it), then set `ATTACHMENT_RETENTION_ENABLED=true`. Also the tenant release-note line.

- [ ] **Step 4: Checklist.** Add a "Screen recording + attachment retention" section to the workspace master checklist: P1 tasks 1–10 (checked as verified), P2/P3 as unchecked with "plan pending (written from the live guide)", and deferred items (Firefox/Safari recording, Cloudinary credit measurement, dev secret rotation).

- [ ] **Step 5: Commit + push docs to the same PR (or a follow-up docs PR if already merged)**

```bash
git add docs/
git commit -m "docs(screen-recording): live-captured FE guide, SOPs, image guide update"
git push
```
