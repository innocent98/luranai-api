"""Vision content-parts injected into the user turn (chat image upload, Task 7)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.rag_service import RAGService

_SALES_RAW = (
    '{"reply":"ok","intent":"GENERAL","signals":{"asked_features":false,'
    '"requested_demo":false,"mentioned_competitor":false,"ready_to_buy":false},'
    '"captured":{"name":null,"email":null,"company":null,"team_size":null,"industry":null}}'
)


def _prompt(text: str = "hi"):
    return SimpleNamespace(full_prompt=text)


def _config():
    return SimpleNamespace(qualification_questions=[])


def _capturing(raw: str = "ok"):
    captured: dict = {}

    async def fake_raw(messages, **kw):
        captured["messages"] = messages
        return raw, 1, False

    return captured, fake_raw


@pytest.mark.asyncio
async def test_ai_image_urls_become_content_parts(monkeypatch):
    svc = RAGService(db=AsyncMock())
    captured, fake = _capturing()
    monkeypatch.setattr(svc, "_call_openai_raw", fake)
    await svc.generate_ai_response(
        _prompt("look"), image_urls=["https://x/a.jpg", "https://x/b.jpg"]
    )
    content = captured["messages"][-1]["content"]
    assert content[0] == {"type": "text", "text": "look"}
    assert [p["image_url"]["url"] for p in content if p["type"] == "image_url"] == [
        "https://x/a.jpg",
        "https://x/b.jpg",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("urls", [None, []])
async def test_ai_no_image_urls_stays_string(monkeypatch, urls):
    svc = RAGService(db=AsyncMock())
    captured, fake = _capturing()
    monkeypatch.setattr(svc, "_call_openai_raw", fake)
    await svc.generate_ai_response(_prompt("hi"), image_urls=urls)
    assert captured["messages"][-1]["content"] == "hi"


@pytest.mark.asyncio
async def test_sales_image_urls_become_content_parts(monkeypatch):
    svc = RAGService(db=AsyncMock())
    captured, fake = _capturing(_SALES_RAW)
    monkeypatch.setattr(svc, "_call_openai_raw", fake)
    await svc.generate_sales_response(_prompt("look"), _config(), image_urls=["https://x/a.jpg"])
    content = captured["messages"][-1]["content"]
    assert content[0] == {"type": "text", "text": "look"}
    assert content[1] == {"type": "image_url", "image_url": {"url": "https://x/a.jpg"}}


@pytest.mark.asyncio
async def test_sales_no_image_urls_stays_string(monkeypatch):
    svc = RAGService(db=AsyncMock())
    captured, fake = _capturing(_SALES_RAW)
    monkeypatch.setattr(svc, "_call_openai_raw", fake)
    await svc.generate_sales_response(_prompt("hi"), _config())
    assert captured["messages"][-1]["content"] == "hi"
