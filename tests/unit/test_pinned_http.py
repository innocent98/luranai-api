"""Pinned outbound transport: DNS is resolved once, the connection goes to the
validated IP, and a hostname that rebinds to an internal address afterwards is
never connected to."""

import shutil
import socket
import ssl
import subprocess
import threading

import httpx
import pytest

from app.utils.pinned_http import (
    UnsafeOutboundURLError,
    apinned_request,
    build_pinned_request,
    pinned_request,
)

PUBLIC = "93.184.216.34"
PUBLIC_B = "8.8.8.8"
METADATA = "169.254.169.254"


def _ai(ip: str):
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    sockaddr = (ip, 0, 0, 0) if ":" in ip else (ip, 0)
    return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr)]


class FakeDNS:
    """First lookup of a name returns its public answer; every later lookup
    returns the rebind answer (what a hostile authoritative server would do)."""

    def __init__(self, table: dict[str, tuple[str, str]]):
        self.table = table
        self.calls: list[str] = []

    def __call__(self, host, *args, **kwargs):
        if host in self.table:
            first, later = self.table[host]
            ip = first if self.calls.count(host) == 0 else later
            self.calls.append(host)
            return _ai(ip)
        # literal IPs resolve to themselves
        self.calls.append(host)
        return _ai(host)


@pytest.fixture
def dns(monkeypatch):
    fake = FakeDNS({})
    monkeypatch.setattr("app.utils.network_security.socket.getaddrinfo", fake)
    return fake


class Recorder:
    def __init__(self, responder=None):
        self.requests: list[httpx.Request] = []
        self.responder = responder or (lambda req: httpx.Response(200, json={"ok": True}))

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responder(request)

    @property
    def sync(self):
        return httpx.MockTransport(self.handler)

    @property
    def hosts(self):
        return [r.url.host for r in self.requests]


# --- build_pinned_request ---------------------------------------------------


def test_build_pins_ip_and_keeps_host_and_sni():
    req = build_pinned_request("POST", "https://api.example.com:8443/x?a=1", PUBLIC, json={"k": 1})
    assert req.url.host == PUBLIC
    assert req.url.port == 8443
    assert req.url.query == b"a=1"
    assert req.headers["host"] == "api.example.com:8443"
    assert req.extensions["sni_hostname"] == "api.example.com"


def test_build_host_header_excludes_userinfo_and_overrides_caller_host():
    req = build_pinned_request(
        "GET", "https://u:secret@api.example.com/x", PUBLIC, headers={"host": "evil.internal"}
    )
    assert req.headers["host"] == "api.example.com"
    assert "secret" not in req.headers["host"]


def test_build_allowlisted_host_connects_by_name_without_sni_override():
    req = build_pinned_request("GET", "http://internal-api/x", "internal-api")
    assert req.url.host == "internal-api"
    assert "sni_hostname" not in req.extensions


def test_build_public_ipv6_is_pinned():
    req = build_pinned_request("GET", "https://v6.example.com/x", "2606:4700:4700::1111")
    assert req.url.host == "2606:4700:4700::1111"
    assert req.headers["host"] == "v6.example.com"


# --- rebinding --------------------------------------------------------------


def test_sync_rebinding_connects_only_to_validated_ip(monkeypatch):
    fake = FakeDNS({"rebind.example.com": (PUBLIC, METADATA)})
    monkeypatch.setattr("app.utils.network_security.socket.getaddrinfo", fake)
    rec = Recorder()
    resp = pinned_request(
        "POST", "https://rebind.example.com/hook", json={"a": 1}, transport=rec.sync
    )
    assert resp.status_code == 200
    assert rec.hosts == [PUBLIC]
    assert METADATA not in rec.hosts
    assert fake.calls == ["rebind.example.com"]  # resolved exactly once
    assert rec.requests[0].headers["host"] == "rebind.example.com"
    assert rec.requests[0].extensions["sni_hostname"] == "rebind.example.com"


async def test_async_rebinding_connects_only_to_validated_ip(monkeypatch):
    fake = FakeDNS({"rebind.example.com": (PUBLIC, METADATA)})
    monkeypatch.setattr("app.utils.network_security.socket.getaddrinfo", fake)
    rec = Recorder()
    transport = httpx.MockTransport(rec.handler)
    resp = await apinned_request("GET", "https://rebind.example.com/hook", transport=transport)
    assert resp.status_code == 200
    assert rec.hosts == [PUBLIC]
    assert fake.calls == ["rebind.example.com"]


def test_private_at_check_time_never_connects(monkeypatch):
    monkeypatch.setattr(
        "app.utils.network_security.socket.getaddrinfo",
        FakeDNS({"x.example.com": (METADATA, METADATA)}),
    )
    rec = Recorder()
    with pytest.raises(UnsafeOutboundURLError):
        pinned_request("GET", "https://x.example.com/", transport=rec.sync)
    assert rec.requests == []


async def test_async_private_at_check_time_never_connects(monkeypatch):
    monkeypatch.setattr(
        "app.utils.network_security.socket.getaddrinfo",
        FakeDNS({"x.example.com": ("10.0.0.5", "10.0.0.5")}),
    )
    rec = Recorder()
    with pytest.raises(UnsafeOutboundURLError):
        await apinned_request(
            "GET", "https://x.example.com/", transport=httpx.MockTransport(rec.handler)
        )
    assert rec.requests == []


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",
        "http://127.0.0.1:8000/",
        "http://localhost/",
        "http://[::1]/",
        "http://[::ffff:169.254.169.254]/",
        "http://[::ffff:127.0.0.1]/",
        "http://[64:ff9b::a9fe:a9fe]/",
        "http://[2002:a9fe:a9fe::1]/",
        "http://100.64.0.1/",
        "ftp://example.com/",
        "file:///etc/passwd",
        "http:///nohost",
        "not a url",
    ],
)
def test_blocked_targets_never_connect(dns, url):
    rec = Recorder()
    with pytest.raises(UnsafeOutboundURLError):
        pinned_request("GET", url, transport=rec.sync)
    assert rec.requests == []


def test_unresolvable_host_fails_closed(monkeypatch):
    def boom(*a, **k):
        raise socket.gaierror("nope")

    monkeypatch.setattr("app.utils.network_security.socket.getaddrinfo", boom)
    rec = Recorder()
    with pytest.raises(UnsafeOutboundURLError):
        pinned_request("GET", "https://nx.example.com/", transport=rec.sync)
    assert rec.requests == []


def test_mixed_answer_with_any_private_ip_is_rejected(monkeypatch):
    def mixed(host, *a, **k):
        return _ai(PUBLIC) + _ai(METADATA)

    monkeypatch.setattr("app.utils.network_security.socket.getaddrinfo", mixed)
    rec = Recorder()
    with pytest.raises(UnsafeOutboundURLError):
        pinned_request("GET", "https://mixed.example.com/", transport=rec.sync)
    assert rec.requests == []


# --- legit public URL -------------------------------------------------------


def test_legit_public_url_params_json_and_headers(dns):
    dns.table["hooks.example.com"] = (PUBLIC, PUBLIC)
    rec = Recorder()
    resp = pinned_request(
        "POST",
        "https://hooks.example.com/in",
        headers={"X-Token": "t"},
        params={"q": "1"},
        json={"a": 1},
        transport=rec.sync,
    )
    assert resp.json() == {"ok": True}
    req = rec.requests[0]
    assert req.method == "POST"
    assert req.headers["x-token"] == "t"
    assert req.url.params["q"] == "1"
    assert req.read() == b'{"a":1}' or b'"a"' in req.read()


def test_allowlisted_host_is_not_pinned(dns):
    rec = Recorder()
    pinned_request(
        "GET", "http://internal-api/x", allowed_hostnames={"internal-api"}, transport=rec.sync
    )
    assert rec.hosts == ["internal-api"]


# --- redirects --------------------------------------------------------------


def _redirector(to: str, code: int = 302):
    return lambda req: httpx.Response(code, headers={"location": to})


def test_redirects_not_followed_by_default(dns):
    dns.table["a.example.com"] = (PUBLIC, PUBLIC)
    rec = Recorder(_redirector("http://169.254.169.254/latest/"))
    resp = pinned_request("GET", "https://a.example.com/", transport=rec.sync)
    assert resp.status_code == 302
    assert rec.hosts == [PUBLIC]


def test_followed_redirect_to_private_host_is_blocked_on_that_hop(dns):
    dns.table["a.example.com"] = (PUBLIC, PUBLIC)
    rec = Recorder(_redirector("http://169.254.169.254/latest/"))
    with pytest.raises(UnsafeOutboundURLError):
        pinned_request("GET", "https://a.example.com/", max_redirects=3, transport=rec.sync)
    assert rec.hosts == [PUBLIC]


def test_followed_redirect_to_rebinding_name_is_pinned_per_hop(dns):
    dns.table["a.example.com"] = (PUBLIC, PUBLIC)
    dns.table["b.example.com"] = (PUBLIC_B, METADATA)

    def responder(req):
        if req.url.host == PUBLIC:
            return httpx.Response(302, headers={"location": "https://b.example.com/next"})
        return httpx.Response(200, text="done")

    rec = Recorder(responder)
    resp = pinned_request("GET", "https://a.example.com/", max_redirects=3, transport=rec.sync)
    assert resp.text == "done"
    assert rec.hosts == [PUBLIC, PUBLIC_B]
    assert rec.requests[1].headers["host"] == "b.example.com"
    assert dns.calls.count("b.example.com") == 1


async def test_async_followed_redirect_to_private_is_blocked(dns):
    dns.table["a.example.com"] = (PUBLIC, PUBLIC)
    rec = Recorder(_redirector("http://[::ffff:10.0.0.1]/"))
    with pytest.raises(UnsafeOutboundURLError):
        await apinned_request(
            "GET",
            "https://a.example.com/",
            max_redirects=3,
            transport=httpx.MockTransport(rec.handler),
        )
    assert rec.hosts == [PUBLIC]


def test_redirect_to_non_http_scheme_is_blocked(dns):
    dns.table["a.example.com"] = (PUBLIC, PUBLIC)
    rec = Recorder(_redirector("file:///etc/passwd"))
    with pytest.raises(UnsafeOutboundURLError):
        pinned_request("GET", "https://a.example.com/", max_redirects=3, transport=rec.sync)


def test_too_many_redirects(dns):
    dns.table["a.example.com"] = (PUBLIC, PUBLIC)
    rec = Recorder(_redirector("https://a.example.com/loop"))
    with pytest.raises(UnsafeOutboundURLError, match="Too many redirects"):
        pinned_request("GET", "https://a.example.com/", max_redirects=2, transport=rec.sync)
    assert len(rec.requests) == 3


def test_cross_origin_redirect_strips_credentials(dns):
    dns.table["a.example.com"] = (PUBLIC, PUBLIC)
    dns.table["b.example.com"] = (PUBLIC_B, PUBLIC_B)

    def responder(req):
        if req.url.host == PUBLIC:
            return httpx.Response(307, headers={"location": "https://b.example.com/"})
        return httpx.Response(200)

    rec = Recorder(responder)
    pinned_request(
        "POST",
        "https://a.example.com/",
        headers={"Authorization": "Bearer s", "X-Keep": "1"},
        json={"a": 1},
        max_redirects=2,
        transport=rec.sync,
    )
    assert "authorization" not in rec.requests[1].headers
    assert rec.requests[1].headers["x-keep"] == "1"
    assert rec.requests[1].method == "POST"  # 307 preserves method


def test_303_downgrades_to_get_without_body(dns):
    dns.table["a.example.com"] = (PUBLIC, PUBLIC)

    def responder(req):
        if req.method == "POST":
            return httpx.Response(303, headers={"location": "/done"})
        return httpx.Response(200)

    rec = Recorder(responder)
    pinned_request(
        "POST", "https://a.example.com/", json={"a": 1}, max_redirects=2, transport=rec.sync
    )
    assert rec.requests[1].method == "GET"
    assert rec.requests[1].read() == b""


# --- TLS: certificate is verified against the hostname, not the IP ---------


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl CLI required")
def test_tls_cert_verified_against_hostname_when_connecting_to_ip(tmp_path):
    cert, key = tmp_path / "c.pem", tmp_path / "k.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "2",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-subj",
            "/CN=api.pinned.test",
            "-addext",
            "subjectAltName=DNS:api.pinned.test",
        ],
        check=True,
        capture_output=True,
    )
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(str(cert), str(key))
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)
    port = listener.getsockname()[1]

    def serve():
        for _ in range(2):
            conn, _addr = listener.accept()
            try:
                with server_ctx.wrap_socket(conn, server_side=True) as tls:
                    tls.recv(4096)
                    tls.sendall(
                        b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok"
                    )
            except (ssl.SSLError, OSError):
                pass

    threading.Thread(target=serve, daemon=True).start()

    def client_for():
        ctx = ssl.create_default_context(cafile=str(cert))
        return httpx.Client(verify=ctx, timeout=5)

    # Right hostname, connecting to the pinned IP: verifies and succeeds.
    ok = build_pinned_request("GET", f"https://api.pinned.test:{port}/", "127.0.0.1")
    with client_for() as c:
        assert c.send(ok).text == "ok"

    # Same IP, but the URL names a host the cert does not cover: must fail.
    bad = build_pinned_request("GET", f"https://other.pinned.test:{port}/", "127.0.0.1")
    with client_for() as c, pytest.raises(httpx.ConnectError):
        c.send(bad)
    listener.close()
