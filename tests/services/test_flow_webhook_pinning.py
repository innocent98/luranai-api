"""Flow WEBHOOK node: outbound call is pinned to the validated IP (no DNS rebinding)."""

import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.services.flow_engine import FlowEngine

PUBLIC = "93.184.216.34"


def _engine():
    engine = FlowEngine(db=MagicMock())
    engine.execute_node = AsyncMock(return_value={"continued": True})
    engine._get_next_node = MagicMock(return_value=None)
    return engine


def _execution():
    return SimpleNamespace(collected_data={}, current_node_id="n1")


def _node(url, method="POST", **extra):
    return {"id": "n1", "config": {"url": url, "method": method, "body": {"a": 1}, **extra}}


def _dns(monkeypatch, name, answers):
    calls: list[str] = []

    def fake(host, *a, **k):
        if host == name:
            ip = answers[min(len(calls), len(answers) - 1)]
            calls.append(host)
        else:
            ip = host
        family = socket.AF_INET6 if ":" in ip else socket.AF_INET
        sockaddr = (ip, 0, 0, 0) if ":" in ip else (ip, 0)
        return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr)]

    monkeypatch.setattr("app.utils.network_security.socket.getaddrinfo", fake)
    return calls


def _ok():
    return httpx.Response(200, json={"hello": "world"})


async def test_webhook_rebinding_host_connects_only_to_validated_ip(monkeypatch):
    calls = _dns(monkeypatch, "rebind.example.com", [PUBLIC, "169.254.169.254"])
    engine, execution = _engine(), _execution()
    with patch("httpx.AsyncClient.send", return_value=_ok()) as send:
        result = await engine._exec_webhook(execution, _node("https://rebind.example.com/in"))
    assert result == {"continued": True}
    sent = send.call_args[0][0]
    assert sent.url.host == PUBLIC
    assert sent.headers["host"] == "rebind.example.com"
    assert sent.extensions["sni_hostname"] == "rebind.example.com"
    assert len(calls) == 1
    assert execution.collected_data["webhook_response"]["success"] is True


@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "DELETE"])
async def test_webhook_methods_still_work(monkeypatch, method):
    _dns(monkeypatch, "api.example.com", [PUBLIC])
    engine, execution = _engine(), _execution()
    with patch("httpx.AsyncClient.send", return_value=_ok()) as send:
        await engine._exec_webhook(execution, _node("https://api.example.com/in", method=method))
    sent = send.call_args[0][0]
    assert sent.method == method
    assert (method in {"POST", "PUT"}) == bool(sent.content)


@pytest.mark.parametrize(
    "url", ["http://169.254.169.254/latest/", "http://[::ffff:10.0.0.1]/", "http://localhost/x"]
)
async def test_webhook_internal_target_blocked(url):
    engine, execution = _engine(), _execution()
    with patch("httpx.AsyncClient.send") as send:
        result = await engine._exec_webhook(execution, _node(url))
    send.assert_not_called()
    assert "Unsafe webhook URL" in str(result)
    assert "webhook_response" not in execution.collected_data


async def test_webhook_redirect_not_followed(monkeypatch):
    _dns(monkeypatch, "api.example.com", [PUBLIC])
    engine, execution = _engine(), _execution()
    redirect = httpx.Response(302, headers={"location": "http://169.254.169.254/"})
    with patch("httpx.AsyncClient.send", return_value=redirect) as send:
        await engine._exec_webhook(execution, _node("https://api.example.com/in"))
    assert send.call_count == 1
    assert execution.collected_data["webhook_response"]["status_code"] == 302
