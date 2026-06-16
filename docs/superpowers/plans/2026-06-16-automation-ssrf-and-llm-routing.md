# Automation SSRF Fix + LLM Action Routing — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the SSRF DNS-rebinding gap in tenant action execution, and replace keyword/regex chat-action intent matching with OpenAI tool-calling.

**Architecture:** Fix 1 adds a resolve-once-then-pin helper in `network_security` and makes `AutomationService.execute_action` connect to the validated IP (preserving Host + TLS SNI). Fix 2 adds an OpenAI tool-calling router in `RAGService` and rewires `ChatAutomationOrchestrator` to route via that router, deleting the keyword/regex intent code while keeping tier gating, the multi-turn collection loop, lookup chains, approval/escalation, and result narration.

**Tech Stack:** Python 3.11, FastAPI, SQLAlchemy, httpx ^0.27, OpenAI SDK (`openai.AsyncOpenAI`, `gpt-4o-mini`), pytest + pytest-asyncio (`asyncio_mode = "auto"`), pytest-mock.

**Spec:** `docs/superpowers/specs/2026-06-16-automation-ssrf-and-llm-routing-design.md`

**Branch:** create `feature/automation-ssrf-llm-routing` off `develop` before Task 1.

**Commit convention:** Conventional Commits. **Do NOT add a Claude co-author trailer** (project rule).

---

## File Structure

| File | Responsibility | Action |
|------|----------------|--------|
| `app/utils/network_security.py` | DNS resolution + SSRF validation; new resolve-and-pin helper | Modify |
| `app/services/automation_service.py` | Connect to pinned IP in `execute_action` | Modify |
| `app/services/rag_service.py` | New `route_action_via_tools` + `ToolRoutingResult` | Modify |
| `app/services/chat_automation_orchestrator.py` | Route via tool-calling; delete keyword/regex intent code | Modify |
| `tests/unit/test_network_security.py` | Helper unit tests incl. rebinding | Modify |
| `tests/services/test_automation_ssrf_pinning.py` | Pinned-IP execution test | Create |
| `tests/services/test_rag_action_routing.py` | Router unit tests (mock OpenAI) | Create |
| `tests/services/test_chat_automation_orchestrator.py` | Orchestrator routing tests | Create |

---

## Task 0: Branch

- [ ] **Step 1: Create the feature branch**

Run:
```bash
git checkout develop && git pull --ff-only && git checkout -b feature/automation-ssrf-llm-routing
```
Expected: `Switched to a new branch 'feature/automation-ssrf-llm-routing'`

- [ ] **Step 2: Commit the spec (already written, currently untracked)**

```bash
git add docs/superpowers/specs/2026-06-16-automation-ssrf-and-llm-routing-design.md docs/superpowers/plans/2026-06-16-automation-ssrf-and-llm-routing.md
git commit -m "docs: spec + plan for automation SSRF fix and LLM action routing"
```

---

## Task 1: `resolve_and_validate_host` helper (SSRF resolve-once)

**Files:**
- Modify: `app/utils/network_security.py`
- Test: `tests/unit/test_network_security.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_network_security.py`:
```python
from unittest.mock import patch

from app.utils.network_security import resolve_and_validate_host


def _addrinfo(ip: str):
    # Mirrors socket.getaddrinfo return shape: (family, type, proto, canonname, sockaddr)
    import socket

    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    sockaddr = (ip, 0, 0, 0) if ":" in ip else (ip, 0)
    return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr)]


def test_resolve_and_validate_returns_public_ip():
    with patch("app.utils.network_security.socket.getaddrinfo", return_value=_addrinfo("93.184.216.34")):
        assert resolve_and_validate_host("example.com") == "93.184.216.34"


def test_resolve_and_validate_rejects_private_ip():
    with patch("app.utils.network_security.socket.getaddrinfo", return_value=_addrinfo("10.0.0.5")):
        with pytest.raises(ValueError):
            resolve_and_validate_host("internal.example.com")


def test_resolve_and_validate_rejects_when_any_ip_unsafe():
    infos = _addrinfo("93.184.216.34") + _addrinfo("127.0.0.1")
    with patch("app.utils.network_security.socket.getaddrinfo", return_value=infos):
        with pytest.raises(ValueError):
            resolve_and_validate_host("rebind.example.com")


def test_resolve_and_validate_allowlisted_hostname_skips_resolution():
    # Allowlisted hostnames bypass IP checks and return the hostname itself.
    assert resolve_and_validate_host("internal-api", allowed_hostnames={"internal-api"}) == "internal-api"


def test_resolve_and_validate_rejects_localhost():
    with pytest.raises(ValueError):
        resolve_and_validate_host("localhost")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `poetry run pytest tests/unit/test_network_security.py -v`
Expected: FAIL — `ImportError: cannot import name 'resolve_and_validate_host'`

- [ ] **Step 3: Implement the helper and refactor the existing validator to use it**

Replace the body of `app/utils/network_security.py` below the imports with:
```python
def _is_disallowed_ip(ip_str: str) -> bool:
    """Return True if IP is not safe for outbound tenant-controlled traffic."""
    ip_obj = ipaddress.ip_address(ip_str)
    return any(
        [
            ip_obj.is_private,
            ip_obj.is_loopback,
            ip_obj.is_link_local,
            ip_obj.is_multicast,
            ip_obj.is_reserved,
            ip_obj.is_unspecified,
        ]
    )


def _resolve_ips(hostname: str) -> set[str]:
    try:
        addrinfo = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise ValueError(f"Could not resolve hostname: {hostname}") from exc

    resolved_ips: set[str] = set()
    for entry in addrinfo:
        sockaddr = entry[4]
        if not sockaddr:
            continue
        ip = sockaddr[0]
        if ip:
            resolved_ips.add(str(ip))

    if not resolved_ips:
        raise ValueError(f"No IP addresses resolved for hostname: {hostname}")
    return resolved_ips


def _reject_unsafe_hostname(hostname: str) -> None:
    if hostname in {"localhost"} or hostname.endswith(".local"):
        raise ValueError("Localhost/internal hostnames are not allowed")


def resolve_and_validate_host(
    hostname: str, allowed_hostnames: set[str] | None = None
) -> str:
    """
    Resolve a hostname ONCE, validate every resolved IP, and return a single
    safe IP to connect to. Callers must connect to the returned value (not the
    hostname) to prevent DNS-rebinding between check and request.

    Allowlisted hostnames bypass IP checks and are returned unchanged so the
    caller connects by hostname (used for internal allowlisted endpoints).

    Raises:
        ValueError: if the hostname is internal, unresolvable, or resolves to
        any disallowed IP range.
    """
    hostname = hostname.strip().lower()
    allowed_hostnames = allowed_hostnames or set()
    if hostname in allowed_hostnames:
        return hostname

    _reject_unsafe_hostname(hostname)

    resolved_ips = _resolve_ips(hostname)
    for ip_str in resolved_ips:
        if _is_disallowed_ip(ip_str):
            raise ValueError(f"URL resolves to disallowed IP: {ip_str}")

    # All resolved IPs are safe; pin a single deterministic address.
    return sorted(resolved_ips)[0]


def ensure_safe_outbound_url(url: str, allowed_hostnames: set[str] | None = None) -> None:
    """
    Validate outbound URL for SSRF protection. Kept for callers that only need
    a yes/no gate. New code should prefer resolve_and_validate_host + pinning.
    """
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Only http/https URLs are allowed")
    if not parsed.hostname:
        raise ValueError("URL hostname is required")
    resolve_and_validate_host(parsed.hostname, allowed_hostnames)
```

Keep the existing module imports (`ipaddress`, `socket`, `from urllib.parse import urlparse`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `poetry run pytest tests/unit/test_network_security.py -v`
Expected: PASS (all, including the pre-existing tests).

- [ ] **Step 5: Commit**

```bash
git add app/utils/network_security.py tests/unit/test_network_security.py
git commit -m "feat(security): add resolve_and_validate_host for SSRF IP pinning"
```

---

## Task 2: Pin the resolved IP in `execute_action`

**Files:**
- Modify: `app/services/automation_service.py` (the request block around lines 427–478)
- Test: `tests/services/test_automation_ssrf_pinning.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/services/test_automation_ssrf_pinning.py`:
```python
"""SSRF pinning: execute_action must connect to the validated IP, not re-resolve."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.services.automation_service import AutomationService


class _FakeAction:
    method = "GET"
    url_template = "https://api.example.com/orders/{{order_id}}"
    headers_template: dict = {}
    query_template: dict = {}
    body_template: dict = {}
    timeout_seconds = 5
    auth_type = type("A", (), {"value": "none"})()
    auth_config: dict = {}


@pytest.mark.asyncio
async def test_execute_pins_validated_ip(monkeypatch):
    service = AutomationService(db=AsyncMock())

    # Capture the request that would be sent.
    captured = {}

    async def fake_send(self, request, **kwargs):
        captured["url"] = str(request.url)
        captured["host_header"] = request.headers.get("host")
        captured["sni"] = request.extensions.get("sni_hostname")
        return httpx.Response(200, json={"ok": True}, request=request)

    monkeypatch.setattr(
        "app.utils.network_security.resolve_and_validate_host",
        lambda host, allowed_hostnames=None: "93.184.216.34",
    )
    monkeypatch.setattr(httpx.AsyncClient, "send", fake_send)

    request = service._build_pinned_request(
        method="GET",
        url="https://api.example.com/orders/42",
        headers={},
        query={},
        body={},
        safe_ip="93.184.216.34",
    )
    assert request.url.host == "93.184.216.34"
    assert request.headers["host"].startswith("api.example.com")
    assert request.extensions.get("sni_hostname") == "api.example.com"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `poetry run pytest tests/services/test_automation_ssrf_pinning.py -v`
Expected: FAIL — `AttributeError: 'AutomationService' object has no attribute '_build_pinned_request'`

- [ ] **Step 3: Add `_build_pinned_request` and use it in `execute_action`**

Add this method to `AutomationService` (near `_apply_auth_headers`):
```python
    def _build_pinned_request(
        self,
        *,
        method: str,
        url: str,
        headers: dict,
        query: dict,
        body: dict,
        safe_ip: str,
    ) -> httpx.Request:
        """
        Build a request that connects to the validated IP while preserving the
        original Host header and TLS SNI/cert hostname. Prevents DNS rebinding
        between validation and connection.
        """
        from urllib.parse import urlparse

        parsed = urlparse(url)
        original_host = parsed.hostname or ""
        host_header = parsed.netloc  # host[:port], used for the Host header

        # If the host is allowlisted, resolve_and_validate_host returns the
        # hostname unchanged — connect by hostname, no pinning needed.
        connect_host = safe_ip if safe_ip != original_host else original_host
        pinned_url = httpx.URL(url).copy_with(host=connect_host)

        request_headers = dict(headers or {})
        request_headers["Host"] = host_header

        request = httpx.Request(
            method=method,
            url=pinned_url,
            headers=request_headers,
            params=query if query else None,
            json=body if method in {"POST", "PUT", "PATCH", "DELETE"} else None,
        )
        if connect_host != original_host:
            # Validate the TLS cert against the real hostname, not the IP.
            request.extensions["sni_hostname"] = original_host
        return request
```

Then in `execute_action`, replace the SSRF validation + request block. Change the validation call from:
```python
            ensure_safe_outbound_url(
                url,
                allowed_hostnames=settings.get_automation_outbound_host_allowlist(),
            )
```
to:
```python
            from urllib.parse import urlparse

            parsed_target = urlparse(url)
            if parsed_target.scheme not in {"http", "https"}:
                raise ValueError("Only http/https URLs are allowed")
            if not parsed_target.hostname:
                raise ValueError("URL hostname is required")
            safe_ip = resolve_and_validate_host(
                parsed_target.hostname,
                allowed_hostnames=settings.get_automation_outbound_host_allowlist(),
            )
```

And replace the httpx send block:
```python
        started = perf_counter()
        try:
            async with httpx.AsyncClient(timeout=action.timeout_seconds) as client:
                response = await client.request(
                    method=method,
                    url=url,
                    headers=headers,
                    params=query if query else None,
                    json=body if method in {"POST", "PUT", "PATCH", "DELETE"} else None,
                )
```
with:
```python
        started = perf_counter()
        try:
            request = self._build_pinned_request(
                method=method,
                url=url,
                headers=headers,
                query=query,
                body=body,
                safe_ip=safe_ip,
            )
            async with httpx.AsyncClient(
                timeout=action.timeout_seconds, follow_redirects=False
            ) as client:
                response = await client.send(request)
```

Update the import at the top of the file from:
```python
from app.utils.network_security import ensure_safe_outbound_url
```
to:
```python
from app.utils.network_security import resolve_and_validate_host
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `poetry run pytest tests/services/test_automation_ssrf_pinning.py tests/unit/test_network_security.py -v`
Expected: PASS

- [ ] **Step 5: Run the existing automation suite for regressions**

Run: `poetry run pytest tests/ -k "automation" -v`
Expected: PASS (no regressions; live e2e may be skipped without network — that is fine).

- [ ] **Step 6: Commit**

```bash
git add app/services/automation_service.py tests/services/test_automation_ssrf_pinning.py
git commit -m "fix(security): pin validated IP in action execution to stop DNS rebinding"
```

---

## Task 3: `route_action_via_tools` in RAGService

**Files:**
- Modify: `app/services/rag_service.py`
- Test: `tests/services/test_rag_action_routing.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/services/test_rag_action_routing.py`:
```python
"""OpenAI tool-calling router unit tests (mocked OpenAI client)."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.services.rag_service import RAGService, ToolRoutingResult


def _tool_call_response(name: str, args: dict):
    tool_call = SimpleNamespace(
        function=SimpleNamespace(name=name, arguments=json.dumps(args))
    )
    message = SimpleNamespace(tool_calls=[tool_call], content=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _no_tool_response():
    message = SimpleNamespace(tool_calls=None, content="Sure, happy to help.")
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


@pytest.mark.asyncio
async def test_route_returns_tool_and_args(monkeypatch):
    service = RAGService(db=AsyncMock())
    fake_client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(
                create=AsyncMock(return_value=_tool_call_response("cancel_order", {"order_id": "42"}))
            )
        )
    )
    monkeypatch.setattr("app.services.rag_service.openai.AsyncOpenAI", lambda **_: fake_client)

    result = await service.route_action_via_tools(
        messages=[{"role": "user", "content": "please cancel order 42"}],
        tools=[{"type": "function", "function": {"name": "cancel_order", "parameters": {}}}],
    )
    assert isinstance(result, ToolRoutingResult)
    assert result.tool_name == "cancel_order"
    assert result.arguments == {"order_id": "42"}
    assert result.is_api_error is False


@pytest.mark.asyncio
async def test_route_returns_no_tool(monkeypatch):
    service = RAGService(db=AsyncMock())
    fake_client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=AsyncMock(return_value=_no_tool_response()))
        )
    )
    monkeypatch.setattr("app.services.rag_service.openai.AsyncOpenAI", lambda **_: fake_client)

    result = await service.route_action_via_tools(messages=[{"role": "user", "content": "hi"}], tools=[])
    assert result.tool_name is None
    assert result.arguments == {}
    assert result.is_api_error is False


@pytest.mark.asyncio
async def test_route_api_error_flagged(monkeypatch):
    service = RAGService(db=AsyncMock())
    fake_client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=AsyncMock(side_effect=RuntimeError("boom")))
        )
    )
    monkeypatch.setattr("app.services.rag_service.openai.AsyncOpenAI", lambda **_: fake_client)

    result = await service.route_action_via_tools(messages=[{"role": "user", "content": "hi"}], tools=[])
    assert result.is_api_error is True
    assert result.tool_name is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `poetry run pytest tests/services/test_rag_action_routing.py -v`
Expected: FAIL — `ImportError: cannot import name 'ToolRoutingResult'`

- [ ] **Step 3: Implement the dataclass and router method**

Add near the top of `app/services/rag_service.py` (after imports):
```python
from dataclasses import dataclass, field


@dataclass
class ToolRoutingResult:
    """Result of asking the LLM which tenant action (if any) to invoke."""

    tool_name: str | None = None
    arguments: dict = field(default_factory=dict)
    is_api_error: bool = False
```

Add this method to `RAGService`:
```python
    async def route_action_via_tools(
        self,
        *,
        messages: list[dict],
        tools: list[dict],
        model_name: str | None = None,
    ) -> ToolRoutingResult:
        """
        Ask the model whether to call a tenant action tool, and with what args.

        Returns ToolRoutingResult. On any API/parse failure, is_api_error=True
        and no tool — callers fall back to normal RAG.
        """
        import json

        model = model_name or settings.OPENAI_LLM_MODEL
        try:
            await openai_llm_rate_limiter.acquire()
            client = openai.AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
            response = await client.chat.completions.create(
                model=model,
                messages=messages,
                tools=tools or None,
                tool_choice="auto" if tools else "none",
                temperature=0.1,
                max_tokens=300,
            )
            message = response.choices[0].message
            tool_calls = getattr(message, "tool_calls", None)
            if not tool_calls:
                return ToolRoutingResult(tool_name=None, arguments={}, is_api_error=False)
            call = tool_calls[0]
            try:
                args = json.loads(call.function.arguments or "{}")
            except (ValueError, TypeError):
                args = {}
            if not isinstance(args, dict):
                args = {}
            return ToolRoutingResult(
                tool_name=call.function.name, arguments=args, is_api_error=False
            )
        except Exception as exc:  # noqa: BLE001 — any failure => RAG fallback
            logger.warning(f"Action routing LLM call failed: {exc}")
            return ToolRoutingResult(tool_name=None, arguments={}, is_api_error=True)
```

(`logger`, `openai`, `settings`, and `openai_llm_rate_limiter` are already imported in this module.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `poetry run pytest tests/services/test_rag_action_routing.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/services/rag_service.py tests/services/test_rag_action_routing.py
git commit -m "feat(automation): add OpenAI tool-calling action router to RAGService"
```

---

## Task 4: `_build_action_tools` in the orchestrator

**Files:**
- Modify: `app/services/chat_automation_orchestrator.py`
- Test: `tests/services/test_chat_automation_orchestrator.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/services/test_chat_automation_orchestrator.py`:
```python
"""ChatAutomationOrchestrator tool-building + routing tests."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.chat_automation_orchestrator import ChatAutomationOrchestrator


def _action(*, slug, name, description, url_template, body_template=None):
    return SimpleNamespace(
        id="00000000-0000-0000-0000-000000000001",
        slug=slug,
        name=name,
        description=description,
        url_template=url_template,
        query_template={},
        body_template=body_template or {},
        input_schema={},
    )


def test_build_action_tools_shapes_openai_tool():
    orch = ChatAutomationOrchestrator(db=AsyncMock())
    action = _action(
        slug="cancel_order",
        name="Cancel order",
        description="Cancel a customer order by order number",
        url_template="https://api.example.com/orders/{{order_id}}/cancel",
    )
    tools = orch._build_action_tools([action])
    assert len(tools) == 1
    fn = tools[0]["function"]
    assert tools[0]["type"] == "function"
    assert fn["name"] == "cancel_order"
    assert "Cancel" in fn["description"]
    assert "order_id" in fn["parameters"]["properties"]


def test_build_action_tools_excludes_secret_fields():
    orch = ChatAutomationOrchestrator(db=AsyncMock())
    action = _action(
        slug="lookup_account",
        name="Lookup account",
        description="Find an account",
        url_template="https://api.example.com/accounts/{{account_id}}",
        body_template={"api_key": "{{secret_api_key}}"},
    )
    tools = orch._build_action_tools([action])
    props = tools[0]["function"]["parameters"]["properties"]
    assert "account_id" in props
    assert "secret_api_key" not in props
```

- [ ] **Step 2: Run test to verify it fails**

Run: `poetry run pytest tests/services/test_chat_automation_orchestrator.py -v`
Expected: FAIL — `AttributeError: ... has no attribute '_build_action_tools'`

- [ ] **Step 3: Implement `_build_action_tools`**

Add to `ChatAutomationOrchestrator`:
```python
    def _build_action_tools(self, actions: list) -> list[dict]:
        """
        Build OpenAI function-calling tools from tenant actions.

        Parameters come from the action's input_schema when present; otherwise
        from the customer-collectible {{placeholders}} discovered in the action
        templates. Secret/token/key fields are never exposed as parameters.
        """
        tools: list[dict] = []
        for action in actions:
            schema = action.input_schema if isinstance(action.input_schema, dict) else {}
            properties = schema.get("properties") if isinstance(schema, dict) else None
            required = schema.get("required") if isinstance(schema, dict) else None

            if not isinstance(properties, dict) or not properties:
                fields = self._extract_required_fields(action)
                properties = {
                    field: {"type": "string", "description": self._field_prompt_label(field)}
                    for field in fields
                }
                required = fields
            else:
                # Filter out any secret-like fields that slipped into the schema.
                properties = {
                    key: value
                    for key, value in properties.items()
                    if self._looks_customer_collectible(key)
                }
                required = [r for r in (required or []) if r in properties]

            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": action.slug,
                        "description": (action.description or action.name or action.slug)[:1024],
                        "parameters": {
                            "type": "object",
                            "properties": properties,
                            "required": required or [],
                        },
                    },
                }
            )
        return tools
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `poetry run pytest tests/services/test_chat_automation_orchestrator.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/services/chat_automation_orchestrator.py tests/services/test_chat_automation_orchestrator.py
git commit -m "feat(automation): build OpenAI tools from tenant actions"
```

---

## Task 5: Route fresh turns via tool-calling; delete keyword matcher

**Files:**
- Modify: `app/services/chat_automation_orchestrator.py`
- Test: `tests/services/test_chat_automation_orchestrator.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/services/test_chat_automation_orchestrator.py`. Note the mocking
convention used throughout: patch the class method with
`AsyncMock(return_value=...)` so `await ...route_action_via_tools(...)` resolves
to the `ToolRoutingResult` directly (do NOT use a lambda side_effect that returns
a coroutine — it would not be awaited).
```python
from app.db.models.automation import TenantActionStatus
from app.services.rag_service import ToolRoutingResult


def _active_chat_action(**kwargs):
    action = _action(**kwargs)
    action.status = TenantActionStatus.ACTIVE
    action.available_in_ai_chat = True
    action.name = kwargs.get("name", "Action")
    return action


@pytest.mark.asyncio
async def test_no_actions_skips_llm(monkeypatch):
    orch = ChatAutomationOrchestrator(db=AsyncMock())
    monkeypatch.setattr(orch.automation_service, "list_actions", lambda tid: [])
    router = AsyncMock(return_value=ToolRoutingResult())
    monkeypatch.setattr(
        "app.services.chat_automation_orchestrator.RAGService.route_action_via_tools", router
    )
    result = await orch.handle_turn(
        tenant_id="t", session_id="s", user_message="hi",
        session_meta={}, conversation_history=[],
    )
    assert result.handled is False
    router.assert_not_awaited()  # cost guard: no LLM call when no actions


@pytest.mark.asyncio
async def test_no_tool_falls_through_to_rag(monkeypatch):
    action = _active_chat_action(
        slug="cancel_order", name="Cancel order", description="Cancel an order",
        url_template="https://api.example.com/orders/{{order_id}}/cancel",
    )
    orch = ChatAutomationOrchestrator(db=AsyncMock())
    monkeypatch.setattr(orch, "_can_use_ai_chat_automation", lambda tid: True)
    monkeypatch.setattr(orch.automation_service, "list_actions", lambda tid: [action])
    monkeypatch.setattr(
        "app.services.chat_automation_orchestrator.RAGService.route_action_via_tools",
        AsyncMock(return_value=ToolRoutingResult(tool_name=None)),
    )
    result = await orch.handle_turn(
        tenant_id="t", session_id="s", user_message="just browsing, thanks",
        session_meta={}, conversation_history=[],
    )
    assert result.handled is False


@pytest.mark.asyncio
async def test_intent_hit_with_all_args_executes(monkeypatch):
    action = _active_chat_action(
        slug="cancel_order", name="Cancel order", description="Cancel an order",
        url_template="https://api.example.com/orders/{{order_id}}/cancel",
    )
    orch = ChatAutomationOrchestrator(db=AsyncMock())
    monkeypatch.setattr(orch, "_can_use_ai_chat_automation", lambda tid: True)
    monkeypatch.setattr(orch.automation_service, "list_actions", lambda tid: [action])
    monkeypatch.setattr(
        "app.services.chat_automation_orchestrator.RAGService.route_action_via_tools",
        AsyncMock(return_value=ToolRoutingResult(tool_name="cancel_order", arguments={"order_id": "42"})),
    )
    executed = {}

    async def fake_exec(**kwargs):
        executed.update(kwargs["input_payload"])
        return SimpleNamespace(
            status=SimpleNamespace(value="succeeded"), id="e1",
            normalized_output={"data": {"status": "cancelled"}}, response_body=None,
        )

    monkeypatch.setattr(orch.automation_service, "execute_action", AsyncMock(side_effect=fake_exec))
    monkeypatch.setattr(orch.conversation_service, "update_session_meta", lambda *a, **k: None)
    monkeypatch.setattr(orch, "_format_execution_response", AsyncMock(return_value=("Done.", False, None)))

    result = await orch.handle_turn(
        tenant_id="t", session_id="s", user_message="please cancel order 42",
        session_meta={}, conversation_history=[],
    )
    assert result.handled is True
    assert executed.get("order_id") == "42"
```

> The `_action` helper from Task 4 has no `status`/`available_in_ai_chat`/`name`
> attributes; `_active_chat_action` adds them. Keep both helpers.

- [ ] **Step 2: Run test to verify it fails or passes against current code**

Run: `poetry run pytest tests/services/test_chat_automation_orchestrator.py::test_no_actions_skips_llm -v`
Expected: FAIL (current `handle_turn` references the keyword matcher path / RAGService import not present).

- [ ] **Step 3: Rewrite the fresh-turn branch of `handle_turn`**

In `app/services/chat_automation_orchestrator.py`, add the import near the top:
```python
from app.services.rag_service import RAGService
```

Replace the fresh-turn block in `handle_turn` (everything from `actions = [` down to the `return await self._execute_action_from_chat(...)` that closes the non-pending path) with:
```python
        actions = [
            action
            for action in self.automation_service.list_actions(tenant_id)
            if action.status == TenantActionStatus.ACTIVE and action.available_in_ai_chat
        ]
        if not actions:
            return AutomationTurnResult(handled=False)
        if not self._can_use_ai_chat_automation(tenant_id):
            return AutomationTurnResult(handled=False)

        tools = self._build_action_tools(actions)
        routing = await RAGService(self.db).route_action_via_tools(
            messages=self._build_routing_messages(conversation_history, user_message),
            tools=tools,
        )
        if routing.is_api_error or not routing.tool_name:
            return AutomationTurnResult(handled=False)

        action = next((a for a in actions if a.slug == routing.tool_name), None)
        if action is None:
            return AutomationTurnResult(handled=False)

        required_fields = self._extract_required_fields(action)
        collected_inputs = self._validate_routed_inputs(required_fields, routing.arguments)
        missing_fields = [f for f in required_fields if f not in collected_inputs]
        if missing_fields:
            return await self._begin_collection(
                tenant_id=tenant_id,
                session_id=session_id,
                action=action,
                actions=actions,
                collected_inputs=collected_inputs,
                missing_fields=missing_fields,
                session_meta=session_meta,
                tenant_ai_config=tenant_ai_config,
                company_name=company_name,
            )

        return await self._execute_action_from_chat(
            tenant_id=tenant_id,
            session_id=session_id,
            action=action,
            input_payload=collected_inputs,
            session_meta=session_meta,
            tenant_ai_config=tenant_ai_config,
            company_name=company_name,
        )
```

Add these three helpers to the class:
```python
    def _build_routing_messages(
        self, conversation_history: list, user_message: str
    ) -> list[dict]:
        system = (
            "You are an action router for a customer support assistant. "
            "Decide whether the customer's latest message requests one of the "
            "available actions, and if so call exactly one tool with the values "
            "the customer has provided. Only extract values the customer actually "
            "gave. If no action clearly applies, do not call any tool."
        )
        messages: list[dict] = [{"role": "system", "content": system}]
        for item in conversation_history[-6:]:
            role = "assistant" if getattr(item, "role", "user") == "assistant" else "user"
            content = getattr(item, "content", "") or ""
            if content:
                messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": user_message})
        return messages

    def _validate_routed_inputs(
        self, required_fields: list[str], arguments: dict
    ) -> dict[str, str]:
        """Trust only values that pass the same shape guards as before."""
        validated: dict[str, str] = {}
        for field in required_fields:
            raw = arguments.get(field)
            if raw is None:
                continue
            value = self._extract_field_value(field, str(raw))
            if value is not None:
                validated[field] = value
        return validated

    async def _begin_collection(
        self,
        *,
        tenant_id,
        session_id,
        action,
        actions: list,
        collected_inputs: dict,
        missing_fields: list[str],
        session_meta: dict,
        tenant_ai_config,
        company_name,
    ) -> "AutomationTurnResult":
        lookup_plan = self._build_lookup_plan(
            tenant_id=tenant_id,
            target_action=action,
            actions=actions,
            target_missing_fields=missing_fields,
        )
        pending: dict = {
            "action_id": str(action.id),
            "action_name": action.name,
            "collected_inputs": collected_inputs,
            "missing_fields": missing_fields,
        }
        if lookup_plan:
            lookup_missing = [
                f for f in lookup_plan.lookup_required_fields if f not in collected_inputs
            ]
            if lookup_missing:
                pending["missing_fields"] = lookup_missing
                pending["lookup_plan"] = {
                    "lookup_action_id": lookup_plan.lookup_action_id,
                    "target_field": lookup_plan.target_field,
                    "lookup_required_fields": lookup_plan.lookup_required_fields,
                    "remaining_target_fields": lookup_plan.remaining_target_fields,
                }
                missing_fields = lookup_missing
        session_meta["pending_ai_action"] = pending
        self.conversation_service.update_session_meta(session_id, session_meta)
        prompt = await self._generate_missing_field_prompt(
            action=action,
            field_name=missing_fields[0],
            tenant_ai_config=tenant_ai_config,
            company_name=company_name,
            fallback_action_name=action.name,
        )
        return AutomationTurnResult(
            handled=True,
            assistant_text=prompt,
            metadata={
                "automation_orchestrated": True,
                "automation_action_id": str(action.id),
                "automation_action_name": action.name,
                "automation_mode": "collect_input",
                "missing_field": missing_fields[0],
            },
        )
```

Keep `_extract_field_value` (used as the validation guard above). Delete the now-unused keyword intent code: `ACTION_HINT_WORDS`, `STOPWORDS`, `CUSTOMER_FIELD_HINTS` constants that are only referenced by deleted code, `_match_action`, `_tokenize`, `_extract_current_inputs`, and `_extract_embedded_field_value`.

> Note: `_extract_required_fields` uses `_looks_customer_collectible`, which references `CUSTOMER_FIELD_HINTS` — keep `CUSTOMER_FIELD_HINTS` and `_looks_customer_collectible`. Only delete constants that become unreferenced (`ACTION_HINT_WORDS`, `STOPWORDS`). Verify with grep in Step 4.

- [ ] **Step 4: Verify no dangling references to deleted symbols**

Run:
```bash
grep -n "ACTION_HINT_WORDS\|_match_action\|_tokenize\|_extract_embedded_field_value\|_extract_current_inputs\|STOPWORDS" app/ -r
```
Expected: no matches (or only in this plan/spec docs). Fix any remaining references.

- [ ] **Step 5: Run tests**

Run: `poetry run pytest tests/services/test_chat_automation_orchestrator.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add app/services/chat_automation_orchestrator.py tests/services/test_chat_automation_orchestrator.py
git commit -m "feat(automation): route chat actions via LLM tool-calling, drop keyword matcher"
```

---

## Task 6: Multi-turn collection re-interpreted by the LLM

**Files:**
- Modify: `app/services/chat_automation_orchestrator.py` (`_continue_pending_action`)
- Test: `tests/services/test_chat_automation_orchestrator.py`

- [ ] **Step 1: Write the failing test**

Append:
```python
@pytest.mark.asyncio
async def test_pending_turn_fills_field_from_llm(monkeypatch):
    action = _active_chat_action(
        slug="cancel_order", name="Cancel order",
        description="Cancel an order", url_template="https://api.example.com/orders/{{order_id}}/cancel",
    )
    orch = ChatAutomationOrchestrator(db=AsyncMock())
    monkeypatch.setattr(orch, "_can_use_ai_chat_automation", lambda tid: True)
    monkeypatch.setattr(orch.automation_service, "get_action", lambda tenant_id, action_id: action)
    monkeypatch.setattr(
        "app.services.chat_automation_orchestrator.RAGService.route_action_via_tools",
        AsyncMock(return_value=ToolRoutingResult(tool_name="cancel_order", arguments={"order_id": "12345"})),
    )
    executed = {}

    async def fake_exec(**kwargs):
        executed.update(kwargs["input_payload"])
        return SimpleNamespace(
            status=SimpleNamespace(value="succeeded"), id="e1",
            normalized_output={"data": {"status": "cancelled"}}, response_body=None,
        )

    monkeypatch.setattr(orch.automation_service, "execute_action", AsyncMock(side_effect=fake_exec))
    monkeypatch.setattr(orch.conversation_service, "update_session_meta", lambda *a, **k: None)
    monkeypatch.setattr(orch, "_format_execution_response", AsyncMock(return_value=("Done.", False, None)))

    session_meta = {"pending_ai_action": {
        "action_id": str(action.id), "action_name": "Cancel order",
        "collected_inputs": {}, "missing_fields": ["order_id"],
    }}
    result = await orch.handle_turn(
        tenant_id="t", session_id="s", user_message="it's 12345",
        session_meta=session_meta, conversation_history=[],
    )
    assert result.handled is True
    assert executed.get("order_id") == "12345"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `poetry run pytest tests/services/test_chat_automation_orchestrator.py::test_pending_turn_fills_field_from_llm -v`
Expected: FAIL (current `_continue_pending_action` uses `_extract_field_value` on the raw message, not the LLM router).

- [ ] **Step 3: Replace field extraction in `_continue_pending_action`**

In `_continue_pending_action`, replace the single-field extraction:
```python
        field_name = missing_fields[0]
        extracted_value = self._extract_field_value(field_name, user_message)
        if extracted_value is None:
```
with an LLM-driven re-interpretation that still validates shape:
```python
        field_name = missing_fields[0]
        routing = await RAGService(self.db).route_action_via_tools(
            messages=self._build_routing_messages([], user_message),
            tools=self._build_action_tools([action]),
        )
        candidate = None
        if not routing.is_api_error and routing.arguments:
            candidate = routing.arguments.get(field_name)
        if candidate is None:
            candidate = user_message
        extracted_value = self._extract_field_value(field_name, str(candidate))
        if extracted_value is None:
```

The rest of `_continue_pending_action` (lookup-plan resolution, remaining-fields loop, execute) is unchanged.

- [ ] **Step 4: Run tests**

Run: `poetry run pytest tests/services/test_chat_automation_orchestrator.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/services/chat_automation_orchestrator.py tests/services/test_chat_automation_orchestrator.py
git commit -m "feat(automation): re-interpret pending-field turns via LLM router"
```

---

## Task 7: Full regression + lint + type check

**Files:** none (verification)

- [ ] **Step 1: Run the full unit + service suite**

Run: `poetry run pytest tests/unit tests/services -v`
Expected: PASS

- [ ] **Step 2: Run lint + type checks used by CI**

Run: `poetry run black --check app tests && poetry run ruff check app tests && poetry run mypy app/services/chat_automation_orchestrator.py app/services/automation_service.py app/services/rag_service.py app/utils/network_security.py`
Expected: PASS (fix formatting/typing issues if reported, then re-run).

- [ ] **Step 3: Grep for any leftover keyword-matcher references repo-wide**

Run: `grep -rn "ACTION_HINT_WORDS\|_match_action\|_tokenize" app/ tests/`
Expected: no matches.

- [ ] **Step 4: Commit any fixups**

```bash
git add -A && git commit -m "chore: lint/type fixups for automation routing + ssrf pinning"
```

---

## Post-implementation (outside this plan)

- Run a focused security review on the SSRF change (`/security-review` or the `security-review` agent) before opening the PR.
- Open a PR from `feature/automation-ssrf-llm-routing` into `develop`.
- The three execution-engine gaps (idempotency, retry_policy, success_condition) remain a separate spec — see `memory/project_action_call_review.md`.
