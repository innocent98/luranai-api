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
    for p in patches:
        p.start()
    return patches


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
