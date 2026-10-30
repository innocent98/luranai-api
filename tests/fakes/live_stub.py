"""Run the API / arq worker for LIVE captures with the LLM stubbed (never calls OpenAI).

    python -m tests.fakes.live_stub api    --port 8100
    python -m tests.fakes.live_stub worker

The patches are the same ones the unit tests use: the model, retrieval and the automation
orchestrator are replaced, everything else (routing, DB, arq, webhooks, sidecar client) is real.
"""

from __future__ import annotations

import argparse
from unittest.mock import patch


def apply_llm_stub() -> list:
    async def _gen(
        self, prompt, max_tokens=800, model_name=None, system_message=None, image_urls=None
    ):
        if image_urls:  # evidence the vision call received the customer's image(s)
            return f"[stubbed AI] I can see {len(image_urls)} image(s) you sent.", 30, False
        return "[stubbed AI] Our opening hours are 9am to 5pm, Monday to Friday.", 30, False

    async def _retr(self, *, tenant_id, query, max_chunks, score_threshold):
        return [], []

    from app.services.chat_automation_orchestrator import AutomationTurnResult

    async def _auto(self, **kwargs):
        return AutomationTurnResult(handled=False)

    patches = [
        patch("app.services.rag_service.RAGService.generate_ai_response", new=_gen),
        patch("app.services.rag_service.RAGService.retrieve_relevant_chunks", new=_retr),
        patch(
            "app.services.chat_automation_orchestrator.ChatAutomationOrchestrator.handle_turn",
            new=_auto,
        ),
    ]
    patches += apply_media_stub()
    for p in patches:
        p.start()
    return patches


def apply_media_stub() -> list:
    """Moderation + Cloudinary stand-ins: a live run must never reach OpenAI or real Cloudinary.

    Moderation flags an image that is exactly 7x7 pixels (so a capture can show a block);
    everything else passes. The "upload" returns a Cloudinary-shaped URL that is never fetched.
    """
    import io
    import uuid

    async def _moderate(image_bytes, content_type):
        from PIL import Image

        with Image.open(io.BytesIO(image_bytes)) as img:
            if img.size == (7, 7):
                return False, "stub: flagged"
        return True, None

    def _upload(self, file_content, tenant_id, session_id, filename):
        name = uuid.uuid4().hex[:12]
        return (
            f"https://res.cloudinary.com/demo/image/upload/luran/chat_images/{name}.jpg",
            f"luran/chat_images/{name}",
            None,
        )

    return [
        patch("app.api.v1.endpoints.chat.moderate_image", new=_moderate),
        patch("app.utils.cloudinary_client.CloudinaryManager.upload_chat_image", new=_upload),
        *apply_offering_media_stub(_moderate),
    ]


def apply_offering_media_stub(moderate) -> list:
    """Offering photos: the real `upload_offering_image` runs against a fake Cloudinary uploader.

    The uploader returns a Cloudinary-shaped result (never fetched); `destroy` records nothing and
    says "ok". Moderation is the same stand-in as chat (a 7x7 image is flagged).
    """
    import uuid

    def _upload(file, **kwargs):
        folder = kwargs.get("folder")
        public_id = kwargs["public_id"]
        full = f"{folder}/{public_id}" if folder else public_id
        return {
            "secure_url": f"https://res.cloudinary.com/demo/image/upload/v1730000000/{full}.jpg",
            "public_id": full,
            "width": 1200,
            "height": 900,
            "bytes": 240_000 + uuid.uuid4().int % 1000,
        }

    return [
        patch("app.services.offering_images.moderate_image", new=moderate),
        patch("cloudinary.uploader.upload", new=_upload),
        patch("cloudinary.uploader.destroy", new=lambda public_id, **kw: {"result": "ok"}),
    ]


def main() -> None:  # pragma: no cover - manual / capture use
    parser = argparse.ArgumentParser()
    parser.add_argument("role", choices=["api", "worker"])
    parser.add_argument("--port", type=int, default=8100)
    args = parser.parse_args()
    apply_llm_stub()
    if args.role == "api":
        import uvicorn

        uvicorn.run("app.main:app", host="127.0.0.1", port=args.port, log_level="warning")
    else:
        from arq.worker import run_worker

        from app.workers.document_processor import WorkerSettings

        run_worker(WorkerSettings)


if __name__ == "__main__":  # pragma: no cover
    main()
