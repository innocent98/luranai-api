"""Task-named LLM seam for the auditor. One interface; per-task model routing."""

import json

from loguru import logger
from openai import AsyncOpenAI

from app.core.config import settings
from app.utils.rate_limiter import openai_llm_rate_limiter

_client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)

TASK_MODEL_MAP: dict[str, str] = {
    "profile_synthesis": settings.AUDITOR_MODEL_PROFILE,
    "page_classify": settings.AUDITOR_MODEL_CLASSIFY,
    "gap_analysis": settings.AUDITOR_MODEL_GAPS,
    "expected_map": settings.AUDITOR_MODEL_GAPS,
}


def model_for_task(task: str) -> str:
    try:
        return TASK_MODEL_MAP[task]
    except KeyError as exc:
        raise ValueError(f"Unknown auditor LLM task: {task}") from exc


async def complete_json(
    task: str, system: str, user: str, *, temperature: float = 0.2
) -> dict:
    """Call the task's configured model and parse a JSON object response."""
    model = model_for_task(task)
    await openai_llm_rate_limiter.acquire()
    resp = await _client.chat.completions.create(
        model=model,
        temperature=temperature,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    content = resp.choices[0].message.content or "{}"
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        logger.error("Auditor LLM task {} returned non-JSON: {}", task, content[:200])
        return {}
