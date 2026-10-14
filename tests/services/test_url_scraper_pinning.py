"""URL scraper (knowledge import): pinned connections, per-hop redirect validation."""

import socket

import httpx
import pytest
from fastapi import HTTPException

from app.services.url_scraper_service import URLScraperService

PUBLIC = "93.184.216.34"
PUBLIC_B = "8.8.8.8"
HTML = (
    "<html><head><title>T</title></head><body>"
    + ("<p>hello world content</p>" * 20)
    + "</body></html>"
)


def _dns(monkeypatch, table):
    calls: list[str] = []

    def fake(host, *a, **k):
        if host in table:
            answers = table[host]
            ip = answers[min(calls.count(host), len(answers) - 1)]
            calls.append(host)
        else:
            ip = host
        family = socket.AF_INET6 if ":" in ip else socket.AF_INET
        sockaddr = (ip, 0, 0, 0) if ":" in ip else (ip, 0)
        return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr)]

    monkeypatch.setattr("app.utils.network_security.socket.getaddrinfo", fake)
    return calls


@pytest.fixture
def sent(monkeypatch):
    """Capture requests that reach the wire layer; answer via ``responder``."""
    state = {"requests": [], "responder": lambda req: httpx.Response(200, text=HTML)}

    async def fake_send(self, request, **kwargs):
        state["requests"].append(request)
        resp = state["responder"](request)
        resp.request = request
        return resp

    monkeypatch.setattr(httpx.AsyncClient, "send", fake_send)
    return state


async def test_rebinding_host_connects_only_to_validated_ip(monkeypatch, sent):
    calls = _dns(monkeypatch, {"docs.example.com": [PUBLIC, "169.254.169.254"]})
    result = await URLScraperService().scrape_url("https://docs.example.com/page")
    assert result["title"] == "T"
    assert [r.url.host for r in sent["requests"]] == [PUBLIC]
    assert sent["requests"][0].headers["host"] == "docs.example.com"
    assert sent["requests"][0].extensions["sni_hostname"] == "docs.example.com"
    assert calls == ["docs.example.com"]


@pytest.mark.parametrize(
    "url", ["http://169.254.169.254/latest/", "http://[::ffff:127.0.0.1]/", "http://10.0.0.1/"]
)
async def test_internal_target_rejected_without_connecting(sent, url):
    with pytest.raises(HTTPException) as exc:
        await URLScraperService().scrape_url(url)
    assert exc.value.status_code == 400
    assert sent["requests"] == []


async def test_redirect_to_internal_host_blocked_on_that_hop(monkeypatch, sent):
    _dns(monkeypatch, {"docs.example.com": [PUBLIC]})
    sent["responder"] = lambda req: httpx.Response(
        302, headers={"location": "http://169.254.169.254/"}
    )
    with pytest.raises(HTTPException):
        await URLScraperService().scrape_url("https://docs.example.com/page")
    assert [r.url.host for r in sent["requests"]] == [PUBLIC]


async def test_redirect_to_rebinding_name_is_pinned_on_its_own_hop(monkeypatch, sent):
    calls = _dns(
        monkeypatch,
        {"a.example.com": [PUBLIC], "b.example.com": [PUBLIC_B, "169.254.169.254"]},
    )

    def responder(req):
        if req.url.host == PUBLIC:
            return httpx.Response(301, headers={"location": "https://b.example.com/final"})
        return httpx.Response(200, text=HTML)

    sent["responder"] = responder
    result = await URLScraperService().scrape_url("https://a.example.com/start")
    assert result["content_length"] > 100
    assert [r.url.host for r in sent["requests"]] == [PUBLIC, PUBLIC_B]
    assert calls.count("b.example.com") == 1


async def test_too_many_redirects_fails(monkeypatch, sent):
    _dns(monkeypatch, {"a.example.com": [PUBLIC]})
    sent["responder"] = lambda req: httpx.Response(
        302, headers={"location": "https://a.example.com/x"}
    )
    with pytest.raises(HTTPException):
        await URLScraperService().scrape_url("https://a.example.com/start")
    assert len(sent["requests"]) == URLScraperService.MAX_REDIRECTS + 1
