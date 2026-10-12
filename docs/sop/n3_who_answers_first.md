# N3 "Who answers first" tenant setting (2026-10-10)

Branch `feat/n3-who-answers-first` (PR to `develop`; PR link in the PR list once opened). Spec `docs/superpowers/specs/2026-10-10-n3-who-answers-first-design.md`, plan `docs/superpowers/plans/2026-10-10-n3-who-answers-first.md`, FE contract `docs/fe-integration-guide-who-answers-first.md` (captures in `docs/fe-integration-captures/who-answers-first/`).

## What shipped
A per-tenant `first_responder` setting (`ai` | `ai_with_handoff` | `human`), a Human-only chat path that never calls the AI, a customer-facing notice when an escalation is missed (all modes), and `first_responder` in the public widget config / visitor bootstrap. Voice is refused for Human-only tenants.

## Why
A tenant with an empty knowledge base still got endless AI clarifying questions (0 chunks retrieved, `no_retrieval_action=clarify`), and a missed escalation (nobody claimed it within `AGENT_ALERT_WAIT_SECONDS`, default 90 s) left the customer in silence.

## How
| Concern | Decision |
|---|---|
| Setting | `tenant_ai_configs.first_responder` String(20) NOT NULL, server default `'ai'`; `Literal` validated in `app/schemas/tenant.py`. `null` on PUT means unchanged (NOT NULL column). |
| Preset (N3-D2) | `ai_with_handoff` forces `no_retrieval_action` and `low_confidence_action` to `escalate` on save (`app/services/first_responder.py::apply_first_responder_preset`, called from `tenant_service.update_ai_config`). The knobs stay the runtime truth. `ai_with_handoff -> ai` leaves them at `escalate`. Rejected: separate overriding rules (two sources of truth). |
| Human-only chat (N3-D1/D4) | `chat.py::_send_message_impl`: right after the ESCALATED early return and before the flow router and RAG. Missing required name/email -> existing contact-capture flow, whose completion escalates; else `_escalate_to_human` + fixed reply (no LLM), `meta.human_first`. Flows are skipped. `__talk_to_human__` still works. |
| Escalation reason | Stored as the plain text `Human-only mode` (ruling): dashboard and agent app render `escalation_reason` as text, so it doubles as the agent-side label and no app code is needed. The spec's `human_first` reason code is not stored. |
| Refactor (ruling) | Only the AUTO_ESCALATE call site was moved onto the new `_escalate_to_human` helper. The other sites differ in ordering (escalate before the assistant message, history fetched after) and keep inline code, each with a comment saying why. |
| Missed notice (N3-D5) | `app/workers/agent_alerts.py::_post_missed_notice`, only when the conditional UPDATE to MISSED flips exactly one row, so a retried job posts nothing. Persisted via `ConversationService.add_message` (bumps `last_message_at`) and fanned out as a `ticket_missed` frame to the customer only. Email clause omitted (ruling): verified that no customer email exists for agent replies. |
| Voice | `VoiceGate._voice_enabled_by_tenant` returns false for `human`, reusing denial `voice_disabled` (widget already handles it). |
| Defaults (N3-D3) | New tenants and company registration create `human`; lazy AI-config creation in the voice-settings PUT applies the migration rule (0 ACTIVE sources -> `human`). |
| Widget / visitor | `first_responder` added to `PublicWidgetConfigResponse` and the public business profile / bootstrap tenant payload. The minimal private-tenant profile reports the schema default `ai` (documented FE trap; use bootstrap). |

## What's involved
- Migration `alembic/versions/n3_first_responder.py` (revision `n3firstresp`, down `mp70attachpos`).
- Model `app/db/models/tenant.py`; schemas `app/schemas/{tenant,widget,visitor}.py`; services `app/services/{first_responder,tenant_service,company_registration_service,visitor_service}.py`, `app/services/voice/gating.py`.
- Endpoints `app/api/v1/endpoints/{chat,tenant,widget}.py`; worker `app/workers/agent_alerts.py`.
- Tests: `tests/services/test_first_responder.py`, `tests/api/v1/test_ai_config_first_responder.py`, `tests/api/v1/test_chat_human_first.py`, `tests/api/v1/test_widget_public_config_first_responder.py`, `tests/migrations/test_n3_first_responder.py`, plus additions to the agent-alert, gating, visitor-bootstrap and escalation suites.
- Merged with `origin/develop` after #118 (voice `turn_signal` hook in `chat.py`): clean merge, both sides kept.

## Migration effect
1. `ADD COLUMN first_responder ... DEFAULT 'ai'` (under `SET LOCAL lock_timeout='5s'` so it fails fast instead of queueing on a hot table).
2. Data step: every tenant whose config is still `ai` and that has no knowledge source with status `ACTIVE` (stored by NAME, uppercase) becomes `human`. Idempotent. Logs `n3firstresp: N tenant(s) set to first_responder=human` (count only).
3. Effect: those tenants stop getting AI replies on deploy. Their admins need the dashboard card to understand and switch back, so promote BE and dashboard together.
4. Downgrade drops the column: all tenants return to AI behaviour (the human flips are not remembered).

## Verification
- Local CI per `.github/workflows/ci.yml`: ruff 0.8.6 clean, black 24.10.0 clean (239 files), automation/flow regression 14 passed, full suite on Postgres 15 / Redis 7: 1456 passed, 33 skipped.
- Fresh-DB `alembic upgrade head` runs the new revision and logs the count line.
- Live local capture (uvicorn + ARQ worker, `AGENT_ALERT_WAIT_SECONDS=5`): every mode, forced preset, 422, null-unchanged, widget config, bootstrap, Human-only welcome/first reply/escalated follow-up, contact capture, voice denial, and a real missed ticket with the WS frame and persisted message. Details and the NOT LIVE list are in the FE guide's verification table.

## Operate / roll back
- Deploy: staging migrates on merge; record the `n3firstresp` line from the CD log. Before prod, report the affected-tenant count (read-only query) to Adebayo.
- Roll back: revert the PR and `alembic downgrade mp70attachpos` (drops the column). Reverting only the code leaves a harmless unused column.
- Tune: `AGENT_ALERT_WAIT_SECONDS` (default 90) controls when the missed notice fires.

## Follow-ups
- Duplicate-ticket race when two simultaneous first messages escalate the same session (pre-existing behaviour, not introduced here).
- Per-tenant affected count (which tenants the migration flipped) to be reported before the prod promotion.
- Live staging pass after merge (agent ring on a real device, name-required capture, ticket reopen in Human-only mode).
- Remaining escalation call sites could adopt `_escalate_to_human` if their ordering constraints are relaxed.
