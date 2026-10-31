"""Run the API / arq worker for the OFFERINGS AI live capture. The model is a stand-in.

    python -m tests.fakes.live_stub_offerings api    --port 8592
    python -m tests.fakes.live_stub_offerings worker

The model (OpenAI) is replaced by the deterministic stand-in of `scripts/offerings_eval.py`, which
reads the catalog block the real code injected and answers from it; when a customer haggles it
misbehaves on its first attempt, so the price guard, its regeneration and its fallback show up in
the captures. Everything else is the real app: lookup, block, guard, cart, quote, notifications,
the WhatsApp webhook, the arq worker and the fake sidecar. Cloudinary and moderation are the stubs
of `tests/fakes/live_stub.py`. This proves the plumbing, not the real model's behaviour.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


def apply_stubs() -> list:
    sys.path.insert(0, str(ROOT / "scripts"))
    import offerings_eval as ev

    from app.services.chat_automation_orchestrator import AutomationTurnResult
    from tests.fakes.live_stub import apply_media_stub

    standin = ev.StandIn()

    async def _retr(self, *, tenant_id, query, max_chunks, score_threshold):
        return [], []

    async def _auto(self, **kwargs):
        return AutomationTurnResult(handled=False)

    patches = [
        patch("app.services.rag_service.RAGService.generate_ai_response", new=standin.plain),
        patch("app.services.rag_service.RAGService.generate_sales_response", new=standin.sales),
        patch("app.services.rag_service.RAGService.retrieve_relevant_chunks", new=_retr),
        patch(
            "app.services.chat_automation_orchestrator.ChatAutomationOrchestrator.handle_turn",
            new=_auto,
        ),
        *apply_media_stub(),
    ]
    for p in patches:
        p.start()
    return patches


def main() -> None:  # pragma: no cover - manual / capture use
    parser = argparse.ArgumentParser()
    parser.add_argument("role", choices=["api", "worker"])
    parser.add_argument("--port", type=int, default=8592)
    args = parser.parse_args()
    apply_stubs()
    if args.role == "api":
        import uvicorn

        uvicorn.run("app.main:app", host="127.0.0.1", port=args.port, log_level="warning")
    else:
        from arq.worker import run_worker

        from app.workers.document_processor import WorkerSettings

        run_worker(WorkerSettings)


if __name__ == "__main__":  # pragma: no cover
    main()
