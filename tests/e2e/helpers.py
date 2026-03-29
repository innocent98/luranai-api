from __future__ import annotations

import time
from typing import Any

import requests


def assert_ok(response: requests.Response, status_code: int = 200) -> Any:
    assert response.status_code == status_code, response.text
    if response.content:
        return response.json()
    return None


def post_chat(
    client,
    *,
    tenant_id: str,
    user_identifier: str,
    message: str,
    session_id: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"message": message, "user_identifier": user_identifier}
    if session_id:
        payload["session_id"] = session_id
    else:
        payload["tenant_id"] = tenant_id
    response = client.post("/chat/message", json=payload, timeout=90)
    return assert_ok(response)


def unique_user(prefix: str) -> str:
    return f"{prefix}-{int(time.time() * 1000)}@example.com"
