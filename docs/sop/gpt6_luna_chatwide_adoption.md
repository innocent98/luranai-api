# SOP — Adopt gpt-6-luna chat-wide via a capability-aware LLM kwargs builder

**Status:** Shipped on branch `feat/adopt-gpt6-luna-chatwide` (off `develop`, PR → `develop`) ·
code commit `41511d0` (`git log 591e46f..HEAD`; `591e46f` is `develop`'s merge-base, excluded) ·
2026-10-07 · this doc's own commit follows the code commit. **Not yet merged.**
**Area:** `app/services/rag_service.py`, `app/core/config.py`, `.env.example`,
`tests/test_llm_kwargs_builder.py`, `tests/services/test_rag_guardrails.py`,
`tests/test_openai_integration.py`.

---

## What shipped

The chat + voice LLM brain moves from `gpt-4o-mini` to OpenAI's reasoning model **`gpt-6-luna`**,
set through the existing single setting `OPENAI_LLM_MODEL`. Because a reasoning model rejects the
sampling params our call sites hardcoded, the swap is **not** a one-line config change — it adds a
small capability-aware request builder.

| Commit | What it covers |
|---|---|
| `41511d0` | `build_chat_completion_kwargs()` + `is_reasoning_model()`; both call sites use it; config default → `gpt-6-luna`; new `OPENAI_REASONING_EFFORT` (default `low`); `.env.example`; tests. |

## Why

`gpt-6-luna` is vision-capable, higher-quality, and **cheaper** than `gpt-4o-mini`
($0.10/$0.50 vs $0.15/$0.60 per 1M in/out). It also unblocks the upcoming chat image-upload
feature (vision) and automatically upgrades the voice brain (voice reuses the chat path). A live
probe (2026-10-06) confirmed it answers on the **standard `chat.completions` API** — no Responses
migration needed.

**Root problem the code had to solve:** reasoning models reject `temperature`, `top_p`, and
`max_tokens`; they require `max_completion_tokens` + `reasoning_effort`. Our two call sites
(`_call_openai_raw`, `route_action_via_tools`) hardcoded `temperature`/`top_p`/`max_tokens`, so a
naive model swap 400s at runtime. One further OpenAI constraint, **validated live**: function
tools + any non-`none` reasoning effort are rejected in `/v1/chat/completions` — you must set
`reasoning_effort="none"` (or move to `/v1/responses`).

## How

- **`build_chat_completion_kwargs(...)`** (module-level, pure → unit-testable) in
  `rag_service.py` shapes kwargs by model family:
  - reasoning model → omit `temperature`/`top_p`, send `max_completion_tokens` + `reasoning_effort`
    (validated against `none|low|medium|high|xhigh`, falls back to `low`); **if `tools` present,
    force `reasoning_effort="none"`**.
  - classic model (`gpt-4o-mini`, …) → unchanged (`temperature`/`top_p`/`max_tokens`).
- **Both call sites** now build kwargs through it and pass `reasoning_effort=settings.OPENAI_REASONING_EFFORT`.
- **Decision — branch instead of hardcoding luna params:** keeps the env flip a *true rollback*.
  Set `OPENAI_LLM_MODEL=gpt-4o-mini` and the old behavior returns with no code change.
- **`reasoning_effort` default `low`:** chat does not stream today (full JSON response,
  `chat.py:540`), so the user waits for the whole completion; `low` kept total round-trip at ~1.1s
  in the live probe. Not split per-path (YAGNI); tool-routing is the one forced exception (`none`).
- **Rejected alternative:** migrating the tools path to `/v1/responses`. Unnecessary — `none`
  works on `chat.completions`, and the action-router is a classifier that wants zero deliberation.

## What's involved

| Path | Change |
|---|---|
| `app/services/rag_service.py` | `is_reasoning_model()`, `build_chat_completion_kwargs()`; both call sites refactored to use it |
| `app/core/config.py:141` | default `OPENAI_LLM_MODEL="gpt-6-luna"`; new `OPENAI_REASONING_EFFORT="low"` |
| `.env.example` | `OPENAI_LLM_MODEL=gpt-6-luna` + `OPENAI_REASONING_EFFORT=low` |
| `tests/test_llm_kwargs_builder.py` | new — both builder branches, tools-forces-none, invalid-effort fallback, passthroughs |
| `tests/services/test_rag_guardrails.py` | runtime-system-message test made model-aware (was pinning `temperature==0.2`) |
| `tests/test_openai_integration.py` | relaxed env-coupled pinned-model assertion; added effort-validity check |

Unaffected (separate model seams): embeddings (`OPENAI_EMBEDDING_MODEL`), the auditor
(`AUDITOR_MODEL_CLASSIFY`, `app/auditor/llm.py`), token-counter tokenizer hints.

## Verification

- Live probe (throwaway, deleted) against the real key, 2026-10-06: `reasoning_effort=low` ✅;
  json_schema **strict** (sales path) ✅; tools rejected with `low` ❌ but ✅ with `none`;
  effort values = `none|low|medium|high|xhigh`; total round-trip ~1.1s at `none` and `low`.
- `tests/test_llm_kwargs_builder.py` (16) + all suites exercising the two call sites
  (rag guardrails/action-routing, chat automation orchestrator, sales generation/wiring, lead
  capture, chat flow router, flow-action-call regressions) — **82 passed, 2 skipped** run under
  `OPENAI_LLM_MODEL=gpt-6-luna` (the real target) and again under the local default.
- `ruff check app/` + `black --check app/` clean (pinned 0.8.6 / 24.10.0).
- Full `pytest tests/` locally: all non-infra tests green; the only real failure found
  (`test_generate_ai_response_uses_runtime_system_message` pinning `temperature`) was fixed. The
  remaining local failures/errors are Redis/Postgres service-container dependencies (`host=redis`
  unreachable) that CI provides — not reproducible on a bare local shell and not touched by this change.

## Operate / roll back

- **Activate the flip:** done in-branch (commit `a69d31f`). `.env.staging.enc` and
  `.env.production.enc` now set `OPENAI_LLM_MODEL=gpt-6-luna` + `OPENAI_REASONING_EFFORT=low`
  (before/after decrypt diff confirmed *only* those two keys changed; both `.enc` re-verified).
  Takes effect on deploy: staging on merge→`develop`, production on the `develop`→`main` promotion.
- **Roll back:** set `OPENAI_LLM_MODEL=gpt-4o-mini` in the environment's `.env` and redeploy. No
  code change needed — the builder restores classic sampling params for a non-reasoning model.

## Follow-ups

- [x] **Staging + production encrypted envs updated** (commit `a69d31f`) — `gpt-6-luna` + effort
  `low`; only those two keys changed (verified). Takes effect on deploy.
- [ ] Update local dev `.env` to `gpt-6-luna` for parity (developer's machine; not committed).
- [ ] Post-deploy: staging live smoke of chat + voice brain on luna.
- [ ] No FE integration guide needed: the chat/voice response contract (`ChatResponse`) is
  unchanged — the swap is transparent to the frontend.
- [ ] Consider measuring `reasoning_effort="none"` for plain chat if lower latency is wanted;
  keep tool-routing on `none` regardless.
