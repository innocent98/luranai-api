"""Shared fixtures: settings, fake sidecar, tenants/admins, arq capture, LLM stub."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID, uuid4

import httpx
import pytest
from loguru import logger

from app.api.deps import get_current_user
from app.channels.registry import set_linked_device_transport
from app.core.config import settings
from app.db.models.tenant import Tenant, TenantAIConfig, TenantCapabilities
from app.db.models.user import UserRole
from app.db.models.whatsapp import WhatsAppConnection
from app.main import app
from app.schemas.user import UserResponse
from app.services import whatsapp_enqueue
from app.services.whatsapp_service import CONSENT_VERSION
from tests.fakes.fake_sidecar import FakeSidecar

WEBHOOK_SECRET = "test-webhook-secret-current"
WEBHOOK_SECRET_PREVIOUS = "test-webhook-secret-previous"
SIDECAR_TOKEN = "test-sidecar-token"
PHONE_A = "2348000000001"
PHONE_B = "2348000000002"
PHONE_C = "2348000000003"


@pytest.fixture
def wa_settings(monkeypatch):
    for name, value in {
        "WHATSAPP_ENABLED": True,
        "WHATSAPP_SIDECAR_URL": "http://fake-sidecar",
        "WHATSAPP_SIDECAR_TOKEN": SIDECAR_TOKEN,
        "WHATSAPP_WEBHOOK_SECRET": WEBHOOK_SECRET,
        "WHATSAPP_WEBHOOK_SECRET_PREVIOUS": WEBHOOK_SECRET_PREVIOUS,
        "WHATSAPP_CONTACT_PEPPER": "test-pepper-not-a-secret",
        "WHATSAPP_MAX_CONNECTIONS": 3,
        "WHATSAPP_OWNER_PAUSE_MINUTES": 120,
    }.items():
        monkeypatch.setattr(settings, name, value)
    return settings


@pytest.fixture
def fake_sidecar(wa_settings):
    fake = FakeSidecar(token=SIDECAR_TOKEN, max_sessions=3)
    set_linked_device_transport(httpx.ASGITransport(app=fake.app))
    yield fake
    set_linked_device_transport(None)


class Queue:
    """Captures what would be enqueued to arq."""

    def __init__(self) -> None:
        self.events: list[dict] = []
        self.sends: list[dict] = []
        self.images: list[dict] = []
        self.fail_events = False


@pytest.fixture
def queue(monkeypatch):
    q = Queue()

    async def enqueue_event(event: dict) -> None:
        if q.fail_events:
            raise RuntimeError("redis down")
        q.events.append(event)

    async def enqueue_send(
        connection_id: UUID,
        chat_id: UUID,
        text: str,
        key: str,
        guard: bool = False,
        rating_request_id: UUID | None = None,
    ) -> None:
        q.sends.append(
            {
                "connection_id": connection_id,
                "chat_id": chat_id,
                "text": text,
                "key": key,
                "guard": guard,
                "rating_request_id": rating_request_id,
            }
        )

    async def enqueue_send_image(
        connection_id: UUID, chat_id: UUID, attachment_id: UUID, caption: str | None, key: str
    ) -> None:
        q.images.append(
            {
                "connection_id": connection_id,
                "chat_id": chat_id,
                "attachment_id": attachment_id,
                "caption": caption,
                "key": key,
            }
        )

    monkeypatch.setattr(whatsapp_enqueue, "enqueue_event", enqueue_event)
    monkeypatch.setattr(whatsapp_enqueue, "enqueue_send", enqueue_send)
    monkeypatch.setattr(whatsapp_enqueue, "enqueue_send_image", enqueue_send_image)
    return q


def make_tenant(db, *, can_use: bool = True, max_numbers: int = 1, max_conversations=1000):
    prefix = uuid4().hex[:8]
    tenant = Tenant(
        id=uuid4(),
        name=f"t_{prefix}",
        display_name=f"T {prefix}",
        api_key_hash=uuid4().hex,
        subscription_tier="trial",
        subscription_status="trialing",
    )
    db.add(tenant)
    db.flush()
    db.add(
        TenantAIConfig(
            id=uuid4(),
            tenant_id=tenant.id,
            brand_voice={},
            escalation_keywords=[],
            max_response_tokens=250,
            grounding_requirement=True,
        )
    )
    db.add(
        TenantCapabilities(
            id=uuid4(),
            tenant_id=tenant.id,
            can_create_tickets=True,
            can_access_analytics=True,
            can_upload_knowledge=True,
            max_conversations_per_month=max_conversations,
            can_use_whatsapp=can_use,
            max_whatsapp_numbers=max_numbers,
        )
    )
    db.commit()
    # The `client` fixture closes the session after each request, which detaches ORM objects.
    return SimpleNamespace(id=tenant.id)


def login_admin(tenant) -> UserResponse:
    now = datetime.now(UTC)
    user = UserResponse(
        id=uuid4(),
        tenant_id=tenant.id,
        email="owner@example.com",
        full_name="Owner Person",
        role=UserRole.TENANT_ADMIN,
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    app.dependency_overrides[get_current_user] = lambda: user
    return user


def consent_body(phone: str = PHONE_A, **extra) -> dict:
    return {
        "phone_number": phone,
        "label": "Main line",
        "consent": {"version": CONSENT_VERSION, "accepted": True},
        **extra,
    }


def connect(
    db, fake, tenant, phone: str = PHONE_A, *, status: str = "connected"
) -> WhatsAppConnection:
    """Insert a connection row directly (skips the HTTP flow) and mirror it in the fake."""
    from app.channels import identity
    from app.db.models.whatsapp import WhatsAppConnection as C
    from app.utils.datetime import utcnow_aware

    key = f"sk_{uuid4().hex[:16]}"
    conn = C(
        tenant_id=tenant.id,
        status=status,
        phone_e164=phone,
        phone_hmac=identity.phone_hmac(phone),
        session_key=key,
        consent_version=CONSENT_VERSION,
        consent_accepted_at=utcnow_aware(),
    )
    db.add(conn)
    db.commit()
    fake.sessions[key] = {
        "status": status,
        "since_ms": 0,
        "last_disconnect": None,
        "phone_digits": phone,
        "replied_to": set(),
    }
    return conn


def inbound(conn, text="Hi, what are your opening hours?", mid=None, ref="1234567890@lid", **kw):
    return {
        "id": str(uuid4()),
        "at": datetime.now(UTC).isoformat(),
        "session_key": conn.session_key,
        "type": "message.inbound",
        "message_id": mid or f"MID{uuid4().hex[:10]}",
        "from_ref": ref,
        "from_phone_digits": None,
        "push_name": "Ada",
        "kind": "text",
        "text": text,
        **kw,
    }


def owner_typed(
    conn, text="Hi, it's the owner, I'll take this one.", mid=None, ref="1234567890@lid"
):
    return {
        "id": str(uuid4()),
        "at": datetime.now(UTC).isoformat(),
        "session_key": conn.session_key,
        "type": "message.outbound_external",
        "message_id": mid or f"OWN{uuid4().hex[:10]}",
        "to_ref": ref,
        "text": text,
    }


class LLM:
    def __init__(self) -> None:
        self.calls = 0
        self.image_urls: list = []  # the `image_urls` of every vision-capable model call


@pytest.fixture
def llm():
    """Stub the model + retrieval + automation so no test reaches OpenAI."""
    counter = LLM()

    async def _gen(
        self, prompt, max_tokens=800, model_name=None, system_message=None, image_urls=None
    ):
        counter.calls += 1
        counter.image_urls.append(image_urls)
        return "We open 9am to 5pm, Monday to Friday.", 30, False

    async def _retr(self, *, tenant_id, query, max_chunks, score_threshold):
        return [], []

    from app.services.chat_automation_orchestrator import AutomationTurnResult

    async def _auto(self, **kwargs):
        return AutomationTurnResult(handled=False)

    with (
        patch("app.services.rag_service.RAGService.generate_ai_response", new=_gen),
        patch("app.services.rag_service.RAGService.retrieve_relevant_chunks", new=_retr),
        patch(
            "app.services.chat_automation_orchestrator.ChatAutomationOrchestrator.handle_turn",
            new=_auto,
        ),
    ):
        yield counter


@pytest.fixture
def log_lines():
    """Everything loguru emits during the test (to assert secrets never reach logs)."""
    lines: list[str] = []
    sink_id = logger.add(lambda m: lines.append(str(m)), level="DEBUG")
    yield lines
    logger.remove(sink_id)


def png_bytes(size: tuple[int, int] = (8, 8), colour: str = "red") -> bytes:
    """A real (tiny) PNG: the pipeline's Pillow format check must pass on it."""
    import io

    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", size, colour).save(out, format="PNG")
    return out.getvalue()


class MediaStubs:
    """Stand-ins for OpenAI moderation and Cloudinary (tests never reach either)."""

    CLOUDINARY_URL = "https://res.cloudinary.com/demo/image/upload/luran/chat_images/{n}.jpg"

    def __init__(self) -> None:
        self.moderated: list[bytes] = []
        self.uploads: list[dict] = []
        self.destroyed: list[str] = []
        self.verdict: tuple[bool, str | None] = (True, None)
        self.moderation_raises = False
        self.upload_fails = False


@pytest.fixture
def media_stubs(monkeypatch):
    from app.api.v1.endpoints import chat as chat_module

    stubs = MediaStubs()

    async def moderate(image_bytes, content_type):
        stubs.moderated.append(image_bytes)
        if stubs.moderation_raises:
            raise RuntimeError("openai down")
        return stubs.verdict

    def upload(file_content, tenant_id, session_id, filename):
        if stubs.upload_fails:
            return None, None, "Failed to upload image"
        n = len(stubs.uploads) + 1
        stubs.uploads.append(
            {"tenant_id": tenant_id, "session_id": session_id, "bytes": len(file_content)}
        )
        return MediaStubs.CLOUDINARY_URL.format(n=n), f"luran/chat_images/pub{n}", None

    def destroy(public_id, *, resource_type):
        stubs.destroyed.append(public_id)
        return True, None

    monkeypatch.setattr(chat_module, "moderate_image", moderate)
    monkeypatch.setattr(chat_module.cloudinary_manager, "upload_chat_image", upload)
    monkeypatch.setattr(chat_module.cloudinary_manager, "destroy", destroy)
    return stubs


def inbound_image(conn, fake, content: bytes | None = None, caption: str = "", **kw):
    """A `message.inbound` image event whose bytes are staged in the fake sidecar's store."""
    content = content if content is not None else png_bytes()
    media = fake.add_media(conn.session_key, content, kw.pop("mime", "image/png"))
    return inbound(conn, text=caption, kind="image", media=media, **kw)


class MemoryLimiter:
    """In-memory stand-in for the Redis rate limiter (no cross-event-loop connection in tests)."""

    def __init__(self, max_calls: int, window_seconds: int = 3600) -> None:
        self.max_calls, self.window_seconds = max_calls, window_seconds
        self.hits: dict[str, int] = {}

    async def get_current_usage(self, identifier: str) -> dict:
        return {"calls": self.hits.get(identifier, 0)}

    async def acquire(self, identifier: str) -> None:
        self.hits[identifier] = self.hits.get(identifier, 0) + 1


@pytest.fixture(autouse=True)
def image_limiter(monkeypatch):
    from app.services import whatsapp_inbound

    limiter = MemoryLimiter(whatsapp_inbound.IMAGE_RATE_MAX)
    monkeypatch.setattr(whatsapp_inbound, "_image_limiter", limiter)
    return limiter


@pytest.fixture(autouse=True)
def capacity_hook_calls(monkeypatch):
    """The post-create capacity alert opens its own DB session and talks to Redis/SMTP: stub it
    for every WhatsApp test (test_capacity_alert.py restores the real one where it matters)."""
    from app.services import whatsapp_capacity

    calls: list[int] = []
    monkeypatch.setattr(whatsapp_capacity, "schedule_post_create_check", lambda: calls.append(1))
    return calls
