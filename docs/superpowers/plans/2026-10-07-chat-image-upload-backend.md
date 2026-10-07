# Chat Image Upload — Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let chat customers attach images that gpt-6-luna interprets and replies to, let agents see customer images and send images back on takeover, with a public upload endpoint, free OpenAI moderation, Cloudinary storage, and full account-deletion purge.

**Architecture:** Two-step flow — a public `POST /api/v1/chat/upload` validates → moderates (OpenAI, fail-closed) → uploads to Cloudinary → inserts a `message_attachments` row (`message_id` NULL); the send paths then pass `attachment_ids[]`, which get linked to the new message and whose URLs are injected into the luna vision prompt. A dedicated table carries `tenant_id` so the account-deletion purge guard enforces cleanup; a cron reaps unlinked uploads.

**Tech Stack:** FastAPI, SQLAlchemy + Alembic, Pydantic v2, Cloudinary (`app/utils/cloudinary_client.py`), OpenAI (`openai.AsyncOpenAI` omni-moderation + chat.completions), ARQ workers, Redis rate limiter.

**Spec:** `docs/superpowers/specs/2026-10-07-chat-image-upload-backend-design.md`

**Base branch:** build on `feat/adopt-gpt6-luna-chatwide` (PR #100) — it provides `gpt-6-luna` as `OPENAI_LLM_MODEL` and `build_chat_completion_kwargs()`/`is_reasoning_model()` in `app/services/rag_service.py`. Branch this work off #100, or rebase onto `develop` once #100 merges.

## Global Constraints

- **Clean commits — NO AI-attribution trailer/footer** (`Co-Authored-By` / `Claude-Session` / `🤖 Generated`). Author solely Adebayo. Binds every subagent.
- **Reproduce CI locally before pushing:** `poetry run ruff check app/`, `poetry run black --check app/` (pinned ruff 0.8.6 / black 24.10.0), and the relevant `poetry run pytest` suites green. `ruff`/`black` only scope `app/`, but keep `tests/` formatted too.
- **Secrets:** never cat/decrypt `.env`; use `scripts/env.sh` round-trip only. `OPENAI_API_KEY` is read from env.
- **Encrypted columns** (`EncryptedString`/`EncryptedJSON` from `app.core.encryption`) emit as plain text/JSON at the DB level; migrations use the underlying `sa.String`/`sa.Text`/`sa.JSON`.
- **Model calls go through `build_chat_completion_kwargs()`** (PR #100) — never hardcode sampling params.
- **Limits (verbatim):** ≤10 MB/image; ≤3 images/message; formats JPEG, PNG, WebP, non-animated GIF, HEIC (convert to JPEG); strip EXIF/GPS.
- **Moderation is fail-closed:** any moderation error → reject the upload.
- **Gate:** per-tenant `widget_allow_file_upload`, default ON.
- **New `message_attachments` table MUST carry `tenant_id` FK → `tenants.id`** so the purge completeness guard auto-discovers it (then Task 9 wires the delete + seed).
- **Alembic head to chain from:** `ad40accountdeletion`.

## Review Focus

Inputs the spec implies that a naive task set would leave unexercised — each is pinned to a test in the owning task:

- **Image-only message (no text).** A send with `attachment_ids` and empty/absent `message` must be accepted and produce a vision turn. — Task 6.
- **Foreign / already-linked / unknown `attachment_ids`.** attachment belonging to another session, or already attached to a message, or a random UUID → 400, never silently linked or leaking another session's image. — Task 6.
- **Moderation call error (timeout/5xx), not just a flagged image.** Must reject (fail-closed), distinct from a clean pass. — Task 4 + Task 5.
- **Disguised / oversized / non-image upload.** A `.exe` renamed `.png`, a 50 MB file, an animated GIF, a 9000px image → rejected at validation with the right status. — Task 5.
- **Purge resilience when the Cloudinary asset is already gone.** Deleting a `public_id` that no longer exists must not abort the tenant purge; rows still deleted; guard passes. — Task 9.

---

### Task 1: Vision validation probe (throwaway — the §3 gate)

**Goal:** Answer, against the live API, whether `gpt-6-luna` accepts image content-parts via `chat.completions` (our pipeline), and at which `reasoning_effort`. This gates Task 7's approach. **Throwaway — no committed code.**

**Files:** none committed. Write the probe under the session scratchpad, run via `poetry run python`, then delete it.

- [ ] **Step 1: Write the probe** (scratchpad, loads key via `dotenv` from the project `.env`, never prints it) that calls:
```python
# content-parts via chat.completions, effort low then none
await client.chat.completions.create(
    model="gpt-6-luna",
    messages=[{"role": "user", "content": [
        {"type": "text", "text": "Describe this image in one sentence."},
        {"type": "image_url", "image_url": {"url": PUBLIC_TEST_IMAGE_URL}},
    ]}],
    reasoning_effort="low", max_completion_tokens=400,
)
```
Also try `reasoning_effort="none"`, and (fallback) `client.responses.create(model=..., input=[{role:user, content:[{type:input_text},{type:input_image, image_url}]}], reasoning={"effort":"low"})`. Wrap each in try/except; print a SUMMARY of which combos WORK + latency.

- [ ] **Step 2: Run it** and record the result verbatim in the progress ledger. Decision rule:
  - chat.completions + image_url works at `low` → Task 7 builds URL content-parts, effort from `settings.OPENAI_REASONING_EFFORT`.
  - works only at `none` → Task 7 forces `reasoning_effort="none"` for image turns (mirror the tool-calling rule from PR #100).
  - rejected entirely → Task 7 routes image turns through the Responses API seam.
- [ ] **Step 3: Delete the probe.** Record the chosen approach (one line) in the ledger; it is Task 7's input.

---

### Task 2: Cloudinary chat uploader + generic destroy + config

**Files:**
- Modify: `app/utils/cloudinary_client.py` (class `CloudinaryManager`, after `upload_channel_image`, ~`:251`)
- Modify: `app/core/config.py:162` (add `CLOUDINARY_CHAT_FOLDER`)
- Test: `tests/utils/test_cloudinary_chat_upload.py`

**Interfaces — Produces:**
- `CloudinaryManager.upload_chat_image(self, file_content: bytes, tenant_id: UUID, session_id: UUID, filename: str) -> tuple[str | None, str | None, str | None]` → `(secure_url, public_id, error)`.
- `CloudinaryManager.destroy(self, public_id: str) -> tuple[bool, str | None]` → `(ok, error)`; treats "not found" as success.

- [ ] **Step 1: Write failing tests** (mock `cloudinary.uploader`):
```python
from unittest.mock import patch
from uuid import uuid4
from app.utils.cloudinary_client import CloudinaryManager

def test_upload_chat_image_returns_url_and_public_id():
    m = CloudinaryManager(); m.enabled = True
    fake = {"secure_url": "https://res.cloudinary.com/x/chat/t/s/abc.jpg", "public_id": "luran/chat/t/s/abc"}
    with patch("cloudinary.uploader.upload", return_value=fake):
        url, public_id, err = m.upload_chat_image(b"bytes", uuid4(), uuid4(), "p.jpg")
    assert err is None and url == fake["secure_url"] and public_id == fake["public_id"]

def test_destroy_treats_not_found_as_success():
    m = CloudinaryManager(); m.enabled = True
    with patch("cloudinary.uploader.destroy", return_value={"result": "not found"}):
        ok, err = m.destroy("luran/chat/t/s/missing")
    assert ok is True and err is None

def test_upload_chat_image_disabled_returns_error():
    m = CloudinaryManager(); m.enabled = False
    url, public_id, err = m.upload_chat_image(b"b", uuid4(), uuid4(), "p.jpg")
    assert url is None and public_id is None and err is not None
```
- [ ] **Step 2: Run, verify fail** — `poetry run pytest tests/utils/test_cloudinary_chat_upload.py -v` (AttributeError: no `upload_chat_image`).
- [ ] **Step 3: Implement.** Add config `CLOUDINARY_CHAT_FOLDER: str = "luran/chat_images"` at `config.py:162`. Add to `CloudinaryManager`:
```python
def upload_chat_image(self, file_content, tenant_id, session_id, filename):
    if not self.enabled:
        return None, None, "Image upload is not configured"
    try:
        folder = f"{settings.CLOUDINARY_CHAT_FOLDER}/{tenant_id}/{session_id}"
        result = cloudinary.uploader.upload(
            file_content, folder=folder,
            public_id=f"{uuid4().hex}", overwrite=False, resource_type="image",
            # HEIC→jpg, strip metadata (EXIF/GPS), cap dimensions
            format="jpg", transformation=[{"width": 2000, "height": 2000, "crop": "limit"}],
            image_metadata=False, tags=["chat", str(tenant_id)])
        return result.get("secure_url"), result.get("public_id"), None
    except Exception as e:  # noqa: BLE001
        logger.error(f"Chat image upload failed: {e}")
        return None, None, "Failed to upload image"

def destroy(self, public_id):
    if not self.enabled:
        return False, "Cloudinary not configured"
    try:
        res = cloudinary.uploader.destroy(public_id, resource_type="image")
        return (res.get("result") in ("ok", "not found")), None
    except Exception as e:  # noqa: BLE001
        logger.error(f"Cloudinary destroy failed for {public_id}: {e}")
        return False, "Failed to delete image"
```
(Need `from uuid import uuid4` at top if absent.)
- [ ] **Step 4: Run tests; ruff/black `app/`.**
- [ ] **Step 5: Commit** — `feat(cloudinary): chat-image uploader returning public_id + generic destroy`.

---

### Task 3: `message_attachments` model + migration

**Files:**
- Modify: `app/db/models/conversation.py` (add model + enums after `Message`, ~`:240`)
- Create: `alembic/versions/<rev>_add_message_attachments.py`
- Test: `tests/db/test_message_attachment_model.py`

**Interfaces — Produces:**
- `MessageAttachment(Base, UUIDMixin)` table `message_attachments`, columns: `message_id: UUID | None` FK `messages.id` CASCADE (nullable), `session_id: UUID` FK `conversation_sessions.id` CASCADE (not null, indexed), `tenant_id: UUID` FK `tenants.id` CASCADE (not null, indexed), `direction: AttachmentDirection`, `cloudinary_public_id: str`, `url: str` (**EncryptedString**), `content_type: str`, `width: int | None`, `height: int | None`, `bytes: int`, `moderation_status: AttachmentModeration`, `status: AttachmentStatus`, `created_at: datetime`.
- Enums `AttachmentDirection` (`INBOUND`/`OUTBOUND`), `AttachmentStatus` (`UPLOADED`/`LINKED`), `AttachmentModeration` (`PASSED`).

- [ ] **Step 1: Write failing test:**
```python
from app.db.models.conversation import (
    MessageAttachment, AttachmentDirection, AttachmentStatus, AttachmentModeration)

def test_message_attachment_insert_and_tenant_scope(db, seed_session):  # seed_session → ConversationSession
    a = MessageAttachment(
        session_id=seed_session.id, tenant_id=seed_session.tenant_id,
        direction=AttachmentDirection.INBOUND, cloudinary_public_id="luran/chat/x",
        url="https://res.cloudinary.com/x.jpg", content_type="image/jpeg",
        bytes=1234, moderation_status=AttachmentModeration.PASSED,
        status=AttachmentStatus.UPLOADED)
    db.add(a); db.commit(); db.refresh(a)
    assert a.id and a.message_id is None and a.tenant_id == seed_session.tenant_id
```
- [ ] **Step 2: Run, verify fail** (ImportError).
- [ ] **Step 3: Implement model** (mirror `Message` style; `url` uses `EncryptedString`; enums as `SQLEnum(..., name="...")`). Then generate the migration **by hand** (match the repo's explicit style, do NOT autogenerate) chaining `down_revision = "ad40accountdeletion"`, creating the table with `sa.Uuid()` columns, `sa.String`/`sa.Text` for the encrypted `url`, `sa.Enum(..., name=...)` for the three enums, FKs with `ondelete="CASCADE"`, indexes on `session_id`, `tenant_id`, `message_id`, and `PrimaryKeyConstraint('id')`. `downgrade()` drops indexes, table, and the three enum types.
- [ ] **Step 4: Apply + test** — `poetry run alembic upgrade head` (dev DB), `poetry run pytest tests/db/test_message_attachment_model.py -v`. Then `poetry run alembic downgrade -1 && poetry run alembic upgrade head` to prove the migration is reversible.
- [ ] **Step 5: Commit** — `feat(db): message_attachments table + migration`.

---

### Task 4: Moderation service (OpenAI omni-moderation, fail-closed)

**Files:**
- Create: `app/services/image_moderation.py`
- Test: `tests/services/test_image_moderation.py`

**Interfaces — Produces:**
- `async def moderate_image_url(image_url: str) -> tuple[bool, str | None]` → `(allowed, reason)`. `allowed=False` on a flagged image AND on any API error (fail-closed). `reason` is a short, non-leaking label.

- [ ] **Step 1: Write failing tests** (mock `openai.AsyncOpenAI`):
```python
import pytest
from unittest.mock import AsyncMock, patch
from app.services.image_moderation import moderate_image_url

def _resp(flagged):
    class R: results = [type("x", (), {"flagged": flagged})()]
    return R()

@pytest.mark.asyncio
async def test_clean_image_allowed():
    with patch("app.services.image_moderation.openai.AsyncOpenAI") as c:
        c.return_value.moderations.create = AsyncMock(return_value=_resp(False))
        allowed, reason = await moderate_image_url("https://x/a.jpg")
    assert allowed is True and reason is None

@pytest.mark.asyncio
async def test_flagged_image_rejected():
    with patch("app.services.image_moderation.openai.AsyncOpenAI") as c:
        c.return_value.moderations.create = AsyncMock(return_value=_resp(True))
        allowed, reason = await moderate_image_url("https://x/a.jpg")
    assert allowed is False and reason

@pytest.mark.asyncio
async def test_moderation_error_fails_closed():
    with patch("app.services.image_moderation.openai.AsyncOpenAI") as c:
        c.return_value.moderations.create = AsyncMock(side_effect=RuntimeError("boom"))
        allowed, reason = await moderate_image_url("https://x/a.jpg")
    assert allowed is False and reason  # fail-closed
```
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement:**
```python
import openai
from loguru import logger
from app.core.config import settings

_REJECT = "This image can't be accepted. Please try a different one."

async def moderate_image_url(image_url: str) -> tuple[bool, str | None]:
    try:
        client = openai.AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
        resp = await client.moderations.create(
            model="omni-moderation-latest",
            input=[{"type": "image_url", "image_url": {"url": image_url}}])
        if resp.results and resp.results[0].flagged:
            return False, _REJECT
        return True, None
    except Exception as e:  # noqa: BLE001 — fail-closed
        logger.error(f"Image moderation failed (fail-closed): {e}")
        return False, _REJECT
```
- [ ] **Step 4: Run tests; ruff/black.**
- [ ] **Step 5: Commit** — `feat(moderation): fail-closed OpenAI image moderation`.

---

### Task 5: Public customer upload endpoint

**Files:**
- Modify: `app/api/v1/endpoints/chat.py` (add `POST /upload`; mirror `app/api/v1/endpoints/widget.py:149-217`)
- Modify: `app/utils/http_rate_limiter.py` (add an IP-keyed upload limiter dependency for the public endpoint)
- Test: `tests/api/v1/test_chat_image_upload.py`

**Interfaces — Consumes:** `cloudinary_manager.validate_image`, `upload_chat_image` (T2); `moderate_image_url` (T4); `MessageAttachment` (T3).
**Produces:** `POST /api/v1/chat/upload` (multipart `file`, form `session_id`) → `200 {attachment_id, url, content_type, width, height, bytes}`; errors 403 (gate off), 413, 415, 422 `{detail:{code:"image_rejected", reason}}`, 429.

- [ ] **Step 1: Write failing tests** (mock cloudinary + moderation; a seeded session + tenant with `widget_allow_file_upload=True`):
  - clean upload → 200, a `message_attachments` row exists with `status=uploaded`, `message_id` NULL, `direction=inbound`.
  - tenant gate off → 403.
  - `validate_image` returns invalid (wrong type / too big) → 415/413 and NO Cloudinary call, NO row.
  - moderation returns `(False, reason)` → 422 `image_rejected`, NO Cloudinary upload persisted / row rolled back.
  - moderation error path (patched to raise) → 422 (fail-closed).
```python
def test_upload_rejects_when_tenant_flag_off(client, session_flag_off):
    r = client.post("/api/v1/chat/upload", data={"session_id": str(session_flag_off.id)},
                    files={"file": ("a.png", b"\x89PNG...", "image/png")})
    assert r.status_code == 403

def test_clean_upload_creates_unlinked_attachment(client, db, session_on, mock_cloudinary_ok, mock_moderation_ok):
    r = client.post("/api/v1/chat/upload", data={"session_id": str(session_on.id)},
                    files={"file": ("a.png", _PNG_BYTES, "image/png")})
    assert r.status_code == 200
    body = r.json(); assert body["attachment_id"] and body["url"]
    row = db.query(MessageAttachment).filter_by(id=body["attachment_id"]).one()
    assert row.message_id is None and row.status == AttachmentStatus.UPLOADED
```
- [ ] **Step 2: Run, verify fail** (404 route).
- [ ] **Step 3: Implement.** Flow: resolve session → tenant; **check `tenant.widget_allow_file_upload` → 403 if off**; `file.read()`; `validate_image(content, max_size_mb=10)` → 413/415 on error; upload to Cloudinary (`upload_chat_image`); `moderate_image_url(secure_url)` → if not allowed, `cloudinary_manager.destroy(public_id)` and 422; else insert `MessageAttachment` (`direction=INBOUND`, `status=UPLOADED`, `moderation_status=PASSED`, `tenant_id=session.tenant_id`); return body. Add a dependency `chat_upload_rate_limit` in `http_rate_limiter.py` keyed `"ip"` (mirror `create_rate_limit_dependency(upload_rate_limiter, "ip")`) and attach it. Enforce **≤10 MB** before read via `request`/content-length guard, and belt-and-braces after read.
- [ ] **Step 4: Run tests; ruff/black.**
- [ ] **Step 5: Commit** — `feat(chat): public image upload endpoint (validate+moderate+store)`.

---

### Task 6: Send wiring — schema, image-only, attachment linking, response surfacing

**Files:**
- Modify: `app/schemas/conversation.py:62-71` (`MessageRequest`), `:86-106` (`MessageResponse`)
- Modify: `app/services/conversation_service.py` (`add_message` — link attachments) + a linker helper
- Modify: `app/api/v1/endpoints/chat.py` (customer send: accept `attachment_ids`, validate ownership, link, pass image URLs to the LLM call block at `:2124-2141`)
- Test: `tests/api/v1/test_chat_image_send.py`, `tests/services/test_attachment_linking.py`

**Interfaces — Consumes:** `MessageAttachment` (T3). **Produces:**
- `MessageRequest.attachment_ids: list[UUID] | None`; `message` becomes `str | None` with a model-validator: **require non-empty `message` OR at least one `attachment_id`.**
- `MessageResponse.attachments: list[AttachmentOut]` where `AttachmentOut = {id, url, content_type, width, height}`.
- `ConversationService.link_attachments(session_id, message_id, attachment_ids) -> list[MessageAttachment]` — validates each id is in this session, `status=UPLOADED`, unlinked; sets `message_id` + `status=LINKED`; raises `ValueError` otherwise.

- [ ] **Step 1: Write failing tests:**
  - image-only send (`message=""`/omitted + one `attachment_id`) → 200, user message persisted, response `attachments` has the image. (Review Focus #1)
  - `attachment_id` from another session → 400; from an already-linked message → 400; random UUID → 400. (Review Focus #2)
  - normal text+image send → attachments linked (`status=linked`, `message_id` set) and surfaced in `MessageResponse.attachments`.
```python
def test_image_only_message_accepted(client, session_on, uploaded_attachment):
    r = client.post("/api/v1/chat/message", json={
        "session_id": str(session_on.id), "message": "", "attachment_ids": [str(uploaded_attachment.id)]})
    assert r.status_code == 200
    assert r.json()["message"]["attachments"][0]["id"] == str(uploaded_attachment.id)

def test_foreign_attachment_rejected(client, session_on, other_session_attachment):
    r = client.post("/api/v1/chat/message", json={
        "session_id": str(session_on.id), "message": "hi", "attachment_ids": [str(other_session_attachment.id)]})
    assert r.status_code == 400
```
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement.** Schema: add field + `@model_validator(mode="after")` requiring text-or-attachment; add `AttachmentOut` + `attachments` (default `[]`, `from_attributes`). `link_attachments` in `conversation_service`. In `send_message` (`chat.py:540`): after the user `add_message`, call `link_attachments(...)` (catch `ValueError` → `HTTPException(400)`); collect `image_urls = [a.url for a in linked]`; pass `image_urls=image_urls` into `generate_ai_response`/`generate_sales_response` at `:2124-2141`; build `MessageResponse` with `attachments`. If `message` is empty, pass a minimal placeholder prompt text so the string-based prompt assembly stays valid (the image carries the turn).
- [ ] **Step 4: Run tests; ruff/black.**
- [ ] **Step 5: Commit** — `feat(chat): accept + link image attachments on send; image-only messages`.

---

### Task 7: Vision content-parts in rag_service (uses Task 1's decision)

**Files:**
- Modify: `app/services/rag_service.py` — `generate_ai_response` (`:1099-1138`, messages at `:1120-1126`) and `generate_sales_response` (`:1140-`, messages at `:1185`)
- Test: `tests/services/test_rag_vision.py`

**Interfaces — Consumes:** Task 1's chosen approach. **Produces:**
- `generate_ai_response(..., image_urls: list[str] | None = None)` and `generate_sales_response(..., image_urls: list[str] | None = None)` — when `image_urls`, the user turn becomes content-parts.

- [ ] **Step 1: Write failing test** (mock `_call_openai_raw`/client; assert the user message is a content-part list with one `image_url` per url when `image_urls` given, and stays a plain string when not):
```python
@pytest.mark.asyncio
async def test_image_urls_become_content_parts(monkeypatch, rag):
    captured = {}
    async def fake_raw(messages, **kw): captured["messages"] = messages; return ("ok", 1, False)
    monkeypatch.setattr(rag, "_call_openai_raw", fake_raw)
    await rag.generate_ai_response(_prompt("hi"), image_urls=["https://x/a.jpg"])
    user = captured["messages"][-1]
    assert isinstance(user["content"], list)
    assert any(p.get("type") == "image_url" for p in user["content"])

@pytest.mark.asyncio
async def test_no_image_urls_stays_string(monkeypatch, rag):
    captured = {}
    async def fake_raw(messages, **kw): captured["messages"] = messages; return ("ok", 1, False)
    monkeypatch.setattr(rag, "_call_openai_raw", fake_raw)
    await rag.generate_ai_response(_prompt("hi"))
    assert isinstance(captured["messages"][-1]["content"], str)
```
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement** per Task 1's decision. Default (works via chat.completions): build the user content as
  `[{"type":"text","text":prompt.full_prompt}] + [{"type":"image_url","image_url":{"url":u}} for u in image_urls]`.
  If Task 1 found image turns need `reasoning_effort="none"`, thread that through `build_chat_completion_kwargs` for image turns (new param, mirroring the tools rule). If Task 1 found chat.completions rejects vision, add a `_call_openai_vision_via_responses` seam and route image turns there — keep text-only turns unchanged. Apply to BOTH `generate_ai_response` and `generate_sales_response`.
- [ ] **Step 4: Run tests; ruff/black.**
- [ ] **Step 5: Commit** — `feat(rag): inject image content-parts into luna vision turns`.

---

### Task 8: Agent upload + agent-message attachments + fan-out

**Files:**
- Modify: `app/api/v1/endpoints/chat.py` (new authed `POST /sessions/{session_id}/upload`; extend `send_agent_message` at `:2750-2832`)
- Test: `tests/api/v1/test_agent_image_message.py`

**Interfaces — Consumes:** T2, T4, T3, `link_attachments` (T6). **Produces:**
- `POST /api/v1/chat/sessions/{session_id}/upload` — bearer-authed (`get_current_user` + `verify_tenant_access` + role gate `TENANT_ADMIN`/`SUPPORT_AGENT`, mirroring `send_agent_message`), same validate→moderate→store pipeline, `direction=OUTBOUND`. Returns the same body as Task 5.
- `send_agent_message` accepts `attachment_ids` (already on `MessageRequest` from T6); links them to the HUMAN_AGENT message; the `connection_manager.fanout_event` message dict gains `attachments: [{id,url,content_type}]`.

- [ ] **Step 1: Write failing tests:**
  - authed agent upload for a session the agent can access → 200 + row `direction=outbound`, `status=uploaded`.
  - agent upload for a session in a different tenant → 403.
  - `agent-message` with `attachment_ids` → message persisted, attachments linked, and `fanout_event` called with an `attachments` list (assert via a mocked `connection_manager.fanout_event`).
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement** the authed upload (reuse a shared internal helper with Task 5's pipeline to stay DRY — extract `_process_image_upload(file, session, direction) -> MessageAttachment`), and extend `send_agent_message`: after `add_message(role=HUMAN_AGENT, ...)`, `link_attachments(...)`, then add `attachments` to the fanout dict (`:2808-2818`) and to the returned `MessageResponse`.
- [ ] **Step 4: Run tests; ruff/black.**
- [ ] **Step 5: Commit** — `feat(chat): agent image upload + send on takeover with fan-out`.

---

### Task 9: Account-deletion purge integration + completeness-guard seed

**Files:**
- Modify: `app/services/account_purge_service.py` (`purge_tenant`, `:280-361`)
- Modify: `tests/services/test_account_purge_service.py` (`_seed_every_tenant_scoped_table`, `:739`)
- Test: `tests/workers/test_account_deletion_purge.py` (add attachment-purge cases) + the existing completeness guard

**Interfaces — Consumes:** `MessageAttachment` (T3), `cloudinary_manager.destroy` (T2).

- [ ] **Step 1: Write failing tests:**
  - purge of a tenant with 2 attachments deletes both rows AND calls `cloudinary_manager.destroy` once per `public_id` (mock `destroy`).
  - `destroy` returning `(False, "...")` or raising for one asset does NOT abort the purge; the other asset is still attempted; rows still deleted. (Review Focus #5)
  - the existing completeness guard (`test_purge_tenant_completeness_guard...`) passes with `message_attachments` seeded.
```python
def test_purge_deletes_attachments_and_cloudinary(db, purge_service, tenant_with_attachments):
    with patch("app.services.account_purge_service.cloudinary_manager.destroy") as d:
        purge_service.purge_tenant(tenant_with_attachments.id)
    assert d.call_count == 2
    assert db.query(MessageAttachment).filter_by(tenant_id=tenant_with_attachments.id).count() == 0
```
- [ ] **Step 2: Run, verify fail** (completeness guard fails: `message_attachments` has rows after purge / not seeded).
- [ ] **Step 3: Implement.** In `purge_tenant`, BEFORE the `ConversationSession` delete: query `MessageAttachment.cloudinary_public_id` for the tenant, call `cloudinary_manager.destroy(pid)` for each inside a try/except that logs and continues (never aborts), then `self.db.execute(delete(MessageAttachment).where(MessageAttachment.tenant_id == tenant_id))`. Add a `message_attachments` row to `_seed_every_tenant_scoped_table`.
- [ ] **Step 4: Run tests; ruff/black.**
- [ ] **Step 5: Commit** — `feat(purge): delete chat attachments + Cloudinary assets on tenant purge`.

---

### Task 10: Orphan-attachment cleanup cron

**Files:**
- Create: `app/workers/attachment_cleanup.py`
- Modify: `app/workers/document_processor.py` (`WorkerSettings.functions` `:1231-1256`, `cron_jobs` `:1271-`)
- Test: `tests/workers/test_attachment_cleanup.py`

**Interfaces — Produces:** `async def cleanup_orphan_attachments(ctx) -> int` — deletes `message_attachments` with `status=UPLOADED` and `created_at < now - 24h` (+ their Cloudinary assets), returns the count.

- [ ] **Step 1: Write failing test:**
```python
@pytest.mark.asyncio
async def test_cleanup_removes_old_unlinked_only(db, old_unlinked, recent_unlinked, linked):
    with patch("app.workers.attachment_cleanup.cloudinary_manager.destroy") as d:
        n = await cleanup_orphan_attachments({"db": db})
    assert n == 1  # only old_unlinked
    assert db.query(MessageAttachment).filter_by(id=old_unlinked.id).count() == 0
    assert db.query(MessageAttachment).filter_by(id=recent_unlinked.id).count() == 1
    assert db.query(MessageAttachment).filter_by(id=linked.id).count() == 1
```
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement** the worker (select old `UPLOADED` rows, `destroy` each `public_id` tolerating failure, delete rows, return count), import it in `WorkerSettings` `functions`, and add `cron(cleanup_orphan_attachments, hour={3}, minute=0, run_at_startup=False)` to `cron_jobs`.
- [ ] **Step 4: Run tests; ruff/black.**
- [ ] **Step 5: Commit** — `feat(worker): daily orphan chat-attachment cleanup`.

---

## Post-implementation (not a TDD task, done before the FE sub-projects)

- After the branch runs on staging, exercise upload → send → vision reply and agent upload → send end-to-end, capturing **real** request/response bodies (incl. every error shape) into `docs/fe-integration-guide-chat-image-upload.md` (global rule — live-captured, never schema-derived). Reference the captures.
- Write the SOP `docs/sop/chat_image_upload_backend.md`; update the workspace master checklist.

## Self-Review

- **Spec coverage:** §4 model → T3; §5.1 upload → T5; §5.2 send/schema → T6; §5.3 agent → T8; §6 vision seam → T7 (gated by T1); §7 gating/limits/moderation → T2/T4/T5; §8 purge → T9; §9 orphan cleanup → T10; §3 validation → T1; §12 live guide → Post-implementation. ✅
- **Review Focus:** image-only (T6), foreign/linked ids (T6), moderation error fail-closed (T4+T5), disguised/oversized upload (T5), purge tolerates missing asset (T9). ✅
- **Type consistency:** `upload_chat_image → (url, public_id, error)` used consistently T2/T5/T8; `moderate_image_url → (allowed, reason)` T4/T5; `link_attachments` T6 used by T8; `image_urls` param T6→T7; `MessageAttachment` fields consistent T3→T5/T6/T8/T9/T10. ✅
