# Automation hardening: SSRF DNS-rebinding fix + LLM tool-calling for chat action routing

**Date:** 2026-06-16
**Status:** Approved (design)
**Area:** Backend — automation / action_call subsystem (`luran/`)
**Author:** Aubin (with Claude)

## Summary

Two independent fixes to the AI-action (`action_call`) automation subsystem, shipped as one spec:

1. **SSRF DNS-rebinding fix** — close the time-of-check/time-of-use gap where the SSRF guard validates a hostname's IP but `httpx` independently re-resolves it at request time, allowing a rebind to an internal IP.
2. **LLM tool-calling for chat action routing** — replace the brittle keyword/regex intent detection in `ChatAutomationOrchestrator` with OpenAI native function/tool calling, so the AI understands intent by meaning instead of word-counting.

Both are independent and testable separately. Provider is **OpenAI** (`gpt-4o-mini`, `openai.AsyncOpenAI.chat.completions.create`).

## Background / current state (verified 2026-06-15/16)

- `AutomationService.execute_action` (`app/services/automation_service.py:353+`) runs tenant-defined HTTP actions. It calls `ensure_safe_outbound_url(url, allowlist)` (`app/utils/network_security.py`) then makes the request with `httpx.AsyncClient` (line ~471).
- `ChatAutomationOrchestrator` (`app/services/chat_automation_orchestrator.py`) detects when a chat turn should trigger an action. Intent = keyword overlap (`ACTION_HINT_WORDS`, `_match_action`, score ≥ 2); field extraction = regex (`EMAIL_PATTERN`, `IDENTIFIER_PATTERN`, `_extract_field_value`, `_extract_embedded_field_value`).
- Orchestrator is invoked at `app/api/v1/endpoints/chat.py:1772` via `handle_turn(...)`, AFTER flow handling and BEFORE the normal RAG fallback. Returning `handled=False` makes chat fall through to RAG — this is the safety net both fixes rely on.
- RAG/LLM access: `RAGService.generate_ai_response` (`app/services/rag_service.py:937+`) wraps `client.chat.completions.create`. No tool-calling helper exists yet.

## Decisions locked with stakeholder

- **Rollout (intent matching):** HARD REPLACE the keyword matcher with LLM tool-calling. No dual keyword path is retained. When a routing-LLM call fails/times out or returns no tool, the orchestrator returns `handled=False` and chat falls through to normal RAG (existing behavior) — that is the only fallback.
- **Cost/latency guard:** Only invoke the routing LLM when the tenant has ≥1 active `available_in_ai_chat` action (skip entirely otherwise, as today). Use `gpt-4o-mini`.

---

## Fix 1 — SSRF DNS-rebinding (resolve-and-pin)

### Problem (plain terms)
We look up the hostname's IP, confirm it's safe, then hand the *hostname* to httpx, which looks it up **again** and may get a different, malicious IP (e.g. `169.254.169.254`). An attacker controlling the domain's DNS can answer "safe" on the check and "internal" on the real request.

### Approach
Look up the IP **once**, validate it, then force the actual request to connect to **that exact IP**, preserving the `Host` header and TLS certificate verification against the original hostname.

### Changes

**`app/utils/network_security.py`**
- Add `resolve_and_validate_host(hostname: str, allowed_hostnames: set[str] | None = None) -> str` that:
  - performs `getaddrinfo`, applies the existing `_is_disallowed_ip` checks to **every** resolved address,
  - rejects `localhost` / `*.local` / non-http(s) as today,
  - honors the allowlist (allowlisted hostnames bypass IP checks, as today),
  - returns a single validated IP to connect to (prefer first safe address; both IPv4 and IPv6 supported).
- Keep `ensure_safe_outbound_url` as a thin wrapper (or refactor callers) so existing call sites/tests don't silently break. The wrapper validates and discards the IP; the new pinned path uses the returned IP.

**`app/services/automation_service.py` (`execute_action`)**
- Parse the resolved URL; obtain `safe_ip` via `resolve_and_validate_host`.
- Issue the httpx request connecting to `safe_ip` while:
  - keeping the original URL path/query,
  - setting the `Host` header to the original hostname,
  - setting the TLS SNI / cert-verification hostname to the original hostname (httpx `sni_hostname` request extension; verify=True preserved),
  - leaving `follow_redirects=False` (httpx default here) so a 3xx to an internal target is recorded as a non-2xx result, never followed.
- On any resolution/validation failure: set `status=BLOCKED_POLICY`, `error_code="UNSAFE_URL"`, record the redacted resolved request (as today).
- Header redaction, auth injection, timeout, and audit recording are unchanged.

### Out of scope (noted, not done here)
- The execution-engine gaps from the action_call review (idempotency, retry_policy, success_condition) — separate spec.
- Per-redirect re-validation (redirects remain disabled, so unnecessary now).

### Tests
- Rebinding simulation: resolver returns a safe IP at check time and an unsafe IP at request time → request must connect only to the pinned safe IP (assert connection target).
- All-private resolution → `UNSAFE_URL`, `BLOCKED_POLICY`.
- IPv6 disallowed ranges.
- Allowlisted hostname bypass still works.
- HTTPS request to a pinned IP still validates the cert against the hostname (no `verify=False`).
- Existing automation execution tests stay green.

---

## Fix 2 — LLM tool-calling replaces keyword intent matching

### Problem (plain terms)
The AI currently decides which action to run by counting matching words and pulls values with regex. It misses paraphrased intent, false-triggers on casual mentions, and is English-only. We let the model read the message and decide — using OpenAI function calling.

### Approach
Hand the model the tenant's actions as callable tools (typed parameters) plus recent conversation; the model decides whether to act, which action, and what argument values to extract.

### Changes

**`app/services/rag_service.py`**
- Add `async def route_action_via_tools(messages, tools, model=None) -> ToolRoutingResult`:
  - calls `chat.completions.create` with `tools=tools`, `tool_choice="auto"`, low temperature, recent history as `messages`,
  - returns a small dataclass: `tool_name | None`, `arguments: dict`, `is_api_error: bool`,
  - rate-limited via existing `openai_llm_rate_limiter`; wrapped in a timeout consistent with `LLM_REPLY_TIMEOUT_SECONDS`.

**`app/services/chat_automation_orchestrator.py`**
- **Tool builder** `_build_action_tools(actions) -> list[dict]`: for each active `available_in_ai_chat` action, produce an OpenAI tool: `function.name = slug`, `function.description = action.description`, `function.parameters` = JSON Schema derived from `action.input_schema`; fall back to discovered `{{customer-collectible}}` fields (reusing `_extract_required_fields` / `_looks_customer_collectible`) when `input_schema` is absent. Secret/token/key fields remain excluded.
- **`handle_turn` (fresh turn, no pending action):**
  - keep the tenant capability gate (`_can_use_ai_chat_automation`) and the "no actions → `handled=False`" early return (cost guard),
  - build tools, call `route_action_via_tools` with conversation history,
  - if no tool chosen / API error → `handled=False` (→ RAG),
  - if a tool is chosen → resolve the matching action, seed `collected_inputs` with the model-extracted arguments (validated through the existing email/identifier guards before trusting them), then enter the SAME missing-field / lookup-chain / execute path as today.
- **`_continue_pending_action` (multi-turn collection):** re-interpret each follow-up turn by re-calling `route_action_via_tools` with full conversation history and the single pending action as the available tool, accumulating any newly-extracted arguments into `collected_inputs`. This is the one mechanism (no separate regex path), so "what's your order number?" → "12345" reliably fills the arg from context. Keep validation guards, lookup chains, cancel handling, and the `pending_ai_action` session state. If the model still extracts nothing for the outstanding field, re-prompt via the existing `_generate_invalid_field_prompt`.
- **Delete** `ACTION_HINT_WORDS`, `_match_action`, `_tokenize` (if unused elsewhere), `_extract_field_value`, `_extract_embedded_field_value`, and related keyword constants. Keep validation regexes (`EMAIL_PATTERN`, `IDENTIFIER_PATTERN`) as input guards.
- Unchanged: tier gating, lookup chains (`_build_lookup_plan` etc.), approval/escalation, sandbox, and all LLM result-narration (`_generate_llm_execution_reply`, etc.).

### Security invariant (unchanged, defense-in-depth)
The LLM only proposes "which action + which inputs." `execute_action` still enforces `requires_approval`, sensitive-action gating, status checks, `allow_in_flows`, monthly quotas, and the SSRF guard. The model cannot bypass any server-side policy.

### Tests
- Mock OpenAI tool-call responses for: clear intent hit; paraphrased intent ("scrap the thing I bought" → cancel order); casual mention with no tool → `handled=False` → RAG; partial args → collection loop asks for the missing field; multi-turn fill via history; lookup-chain trigger; sensitive action → approval/escalation path.
- Cost guard: tenant with zero AI-chat actions → no LLM routing call made.
- Capability off → `handled=False`.
- API error/timeout in routing → `handled=False` (RAG fallback).

---

## Delivery plan

- Implement with the **`backend-engineer`** agent (FastAPI / OpenAI / security depth), TDD throughout.
- Run a focused **security review** (`/security-review` or `security-review` agent) on the SSRF change before merge.
- Keep all existing flow/action E2E green.
- Branch off the working branch (repo currently reports not-a-git-repo at root; confirm `luran/` git status before branching).
- Commits follow the repo convention: **no Claude co-author trailer** (per project rule).

## Risks

- httpx `sni_hostname` extension behavior across the pinned-IP path must be verified against the installed httpx version (confirm in implementation; add a test that an HTTPS call to a pinned IP validates the cert).
- Tool-calling adds latency on automation turns; mitigated by the actions-exist cost guard and `gpt-4o-mini`.
- JSON Schema generated from `input_schema` may be incomplete for some tenants; fallback to discovered fields covers that, and missing fields still route through the collection loop.
