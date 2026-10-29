# SOP: Conversation counting (6-hour same-conversation window, one shared limit check), 2026-10-29

## What shipped
Branch `feature/conversation-counting-window` -> `develop` (PR linked from the hand-back; **not merged**, the lead merges). Production untouched. **No plan number, plan catalog, migration or pricing copy changed**: this fixes *how* a conversation is counted so the later limit change (Lite/Pro/Growth/trial) lands on a sound meter.

1. **One module owns the meter**: `app/services/conversation_counting.py` (month start, count, limit lookup, `enforce_conversation_limit`, reuse window, reopen rule). The five copy-pasted count queries are gone.
2. **A returning customer continues the same conversation inside a 6-hour window** (`CONVERSATION_REUSE_WINDOW_HOURS`, default 6, `app/core/config.py`) and is not counted again. Abandoned and resolved sessions are reopened in place. Beyond the window a new session is created, which is what counts.
3. **Gaps closed**: `POST /chat/sessions` is gated; voice mint is gated when its session would open a new conversation; reactivating an abandoned/resolved session past the window is a new conversation (and is gated).
4. **Getters made safe**: `get_latest_session_for_visitor/customer` and `get_active_session_for_user` order by `last_message_at desc` and `LIMIT 1` (they used `scalar_one_or_none()` and could raise `MultipleResultsFound`).

## Why
A "conversation" was one `conversation_sessions` row created this month, with no relation to what a customer would call a conversation:
* anonymous widget visitors only reused `ACTIVE`/`ESCALATED`; the worker abandons `ACTIVE` after 30 minutes of silence, so a visitor who came back after a coffee started (and was charged for) a second conversation;
* authed customers never reused `ABANDONED`, and reused `RESOLVED` forever (never recounted);
* `POST /chat/sessions` had no limit check; the same SQL was copy-pasted five times (one copy, `verify_conversation_limit`, was dead code), so any future change had to be made in five places.
Setting limits on top of that would have punished customers for the meter's quirks.

## How (decisions, and what each costs if wrong)
| Decision | Why / rejected alternative | Cost if wrong |
|---|---|---|
| **6 hours**, one setting, measured from the session's **last message** (sliding; AI, agent and customer messages all count) | Long enough for "went to lunch / next shift of the same support thread", short enough that "tomorrow" is a new conversation. Sliding, so a thread that keeps going stays one conversation. Rejected: a calendar-day window (midnight splits a live chat); a fixed window from session start (cuts a live chat in half). | **Too long**: undercounts (one active visitor chatting every 5 h is one conversation forever); tenants get more usage than the plan intends. **Too short**: customers are recounted (the original bug). It is a config value, no migration: change `CONVERSATION_REUSE_WINDOW_HOURS` and restart. |
| **Reopen in place** (abandoned -> active; resolved -> active unless the message is only a widget greeting) rather than creating a linked second row | Keeps the invariant *one row = one counted conversation*, needs no migration and no new column. Rejected: a `reopened_at` counter or a ledger table (migration, and "created_at >= month" is what analytics and the reporting already read). | A reopened resolved thread keeps its old `resolved_at`/rating context; a rating already given stays attached to that conversation. Reporting that counts "resolved this month" can see a conversation resolved twice. |
| **Past the window = new row**, carrying over `tenant_id`, `user_identifier` and `visitor_id` from the old row (never the customer account: that binding still comes only from an authenticated route) | The cleanest way to make "reactivation past the window counts as new" without a migration. Rejected: re-stamping `created_at` of the old row (destroys history and analytics). | The conversation has a new `session_id`. **Widget behaviour**: `luran-chat` only adopts `session_id` from the *first* response, so an old client keeps sending the old id. The server tolerates that (below); the widget should adopt a changed id (FE note). |
| Old-id clients converge: a stale explicit `session_id` is resolved to the customer's/visitor's current reusable session (by visitor, authed customer, or, with neither, the same `user_identifier` among `ACTIVE` sessions) before a new one is created | Without it, a client that keeps the old id would open a new counted conversation on *every* message. | The `user_identifier` fallback only runs when the caller already proved ownership of the old session; a shared identifier (e.g. an API client that sends one constant `user_identifier`) collapses into one conversation. |
| **`ESCALATED` (and `ACTIVE` with an agent) is continued whatever its age** | A human owns it; spawning an AI conversation would orphan the ticket. This is the pre-existing behaviour. | Long-lived escalated threads are never recounted, so a customer can keep a human thread forever at no cost. |
| Widget greeting (`start`, `hi`, `__init__`, ...) on a **resolved** session neither reopens nor recounts it (pre-existing rule, now shared) | The widget sends a greeting on every open; counting it would charge for opening the page. | An abandoned session is still reactivated by a greeting (pre-existing), and past the window that is a new conversation. |
| WhatsApp uses the same rule (`_usable_session`): resolved/abandoned inside the window is reused and reopened, beyond it a new counted session | One rule for every channel. `_human_owns` ignores a resolved session so a reopened thread is the AI's again. | A "thanks" 3 hours after a resolve now reopens the conversation and gets an AI answer (before: a new conversation). |
| Cap check stays at **creation only**; continuing is never blocked | At the cap a customer mid-conversation keeps being served (as before). | Tenants over the cap can still continue existing conversations. |
| Month = UTC calendar month; `created_at >= first of month` (unchanged) | Same meter as before; only the *number of rows created* changes. | A session created 31 Oct and continued 1 Nov counts in October. |
| Voice mint: refused with the chat 402 when its session is past the window **and** the tenant is at the cap | Voice never creates a session, but its turns write through `send_message`, which would open a new one mid-call. | Voice on a fresh in-window session is never gated. |

## What's involved
* New: `app/services/conversation_counting.py`; tests `tests/conversation_counting/`; capture script `scripts/capture_conversation_counting.py`; captures `docs/fe-integration-captures/conversation-counting/`; FE guide `docs/fe-integration-guide-conversation-counting.md`.
* `app/api/v1/endpoints/chat.py` (`_send_message_impl` step 1: stale-session conversion, reuse by customer/visitor with `reuse_window=True`, reopen, `enforce_conversation_limit`; `POST /chat/sessions` gate).
* `app/services/conversation_service.py` (safe getters, `reuse_window` filter), `app/api/v1/endpoints/customer_conversations.py` (`reuse_window=True`), `app/api/v1/endpoints/voice.py` (mint gate).
* `app/services/whatsapp_inbound.py` (`_usable_session`, `_ensure_session`, `_plan_blocks_conversation`, `_human_owns`).
* Replaced copies: `app/api/deps.py` (**dead `verify_conversation_limit` deleted**, it had no callers), `app/services/subscription_validation_service.py`, `app/services/email_notification_service.py`.
* Config: `CONVERSATION_REUSE_WINDOW_HOURS` (default 6). No migration, no new column, no env required.
* Existing tests updated: `tests/whatsapp/test_inbound.py`, `tests/whatsapp/test_rating.py` (they encoded "resolved always starts a new session"; they now age the session past the window).

## Verification
| Check | Result |
|---|---|
| `ruff check app/` 0.8.6, `black --check app/` 24.10.0 (project venv) | clean |
| New tests (window boundary 5.99 h / 6.02 h, anonymous, authed, WhatsApp, cap 402 shape, ungated paths, month/year rollover, getters with multiple rows, greeting rule, setting override, voice mint) | `tests/conversation_counting/`, `tests/voice/test_token_mint.py`, `tests/whatsapp/test_inbound.py` |
| Whole suite against a dedicated Postgres 15 | see the PR description |
| Live HTTP run (real app on local Postgres/Redis, only the model call stubbed): inside-window return keeps the id, beyond-window gets a new id, old-id client converges, 402 on new visitor / `POST /sessions` / beyond-window return, in-window return served at the cap | `docs/fe-integration-captures/conversation-counting/01..09` |
| NOT exercised | the real widget (it does not yet adopt a changed `session_id`), a real LLM, WhatsApp against the real sidecar, production data volume, the dashboard's authed customer thread over a real session |

## Operate / roll back
* **Tuning**: `CONVERSATION_REUSE_WINDOW_HOURS` in the API env, restart. `0`/small values make every return a new conversation; large values undercount.
* **Before the limit numbers change**: expect the monthly count to *drop* for existing tenants once this ships (fewer rows are created). Re-baseline the proposed Lite 1,000 / Pro 5,000 / Growth 10,000 / trial 100 against a few weeks of post-fix counts, not the old row counts.
* **Roll back**: revert the PR; nothing to migrate. Rows created under the new rule are ordinary sessions. Sessions reopened in place stay `active`.

## Follow-ups
* `luran-chat` (`useChat.ts`): adopt `data.session_id` whenever it differs from the stored one (today only the first response is adopted). The server copes without it, at the cost of a fallback lookup.
* The customer dashboard may want to show the previous conversation as a separate thread after a window rollover (it already lists sessions by `last_message_at`).
* Dashboard usage meter: label it "conversations (a returning customer within 6 hours is the same conversation)".
* `GET /subscription/usage`-style readers should call `count_conversations_this_month` / `conversation_usage`; none exist today beyond the five replaced copies.
