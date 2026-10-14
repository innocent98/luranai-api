# Customer credential hardening on chat write paths

**What shipped:** the customer credential is now required to write into an existing conversation over REST, `metadata.customer_user_id` is no longer trusted from public callers, and staff-visible session meta is an allow-list. Branch `fix/customer-credential-hardening` (PR to `develop`). Closes the three Platform security items from the Opus review of #130 (master checklist, Platform section).

## Why
Defence in depth for the public chat write paths. REST accepted a `session_id` without the customer credential the sockets already require, `metadata.customer_user_id` was taken from the request body, and session meta redaction was a block-list that fails open for new keys.

## How
| Area | Change |
|---|---|
| `POST /chat/message` | New route handler `post_chat_message`. With a `session_id` it requires `user_identifier` equal to the session's (constant-time compare) or `metadata.visitor_token` resolving to the session's visitor; else `403 {"detail":"Invalid customer credential"}`. Unknown session stays `404`. It then strips `customer_user_id` from metadata and calls `send_message`. |
| `send_message` | Now a plain trusted function (no route): callers have authenticated already. New kw `authed_customer_id` is the only way a session is bound to a customer account. Callers: public route, `POST /customer/conversations/message` (passes the token-derived id), voice pipeline (unchanged, voice token). |
| `POST /chat/sessions`, new-session branch of `/chat/message` | `customer_user_id` is never read from metadata and is removed from stored `meta`. |
| `/chat/upload`, `/chat/video/sign`, `/chat/video/register` | Optional `user_identifier` or `visitor_token` (form field / JSON field). An explicit credential must match (or be the owning authed customer's token). A request with no explicit credential is accepted unless `CHAT_MEDIA_REQUIRE_CUSTOMER_CREDENTIAL=true`; a non-owning customer cookie is ignored while the flag is off (guest pages send cookies with `credentials: 'include'`). |
| `POST /voice/session/token` | Requires `user_identifier` or `visitor_token` for the session (403 `{"detail":{"reason":"invalid_customer_credential"}}`), also accepts the owning authed customer's token (cookie/Bearer; a non-owning customer with no explicit credential stays 403), enforced now with no flag: the minted token writes user messages through the trusted `send_message`. luran-chat must add the field (see `docs/fe-integration-guide-voice-credential.md`). |
| Session rating submit | Uses the constant-time credential helper. |
| Staff session meta | `redact_staff_session_meta` keeps only `STAFF_VISIBLE_SESSION_META_KEYS` with scalar values. Message meta keeps the `rating_token` block-list (arbitrary shape, FE reads many keys). |

Decision: the media paths are **not** hard-required yet. Live luran-chat (`origin/main` `app/lib/chat-upload.ts`) sends only `session_id` on all three, so a hard requirement would break image and screen-recording uploads. Rejected: shipping the requirement and the luran-chat change together (cross-repo ordering risk) and ignoring the media paths (leaves the same gap). Residual until the flag flips: an unlinked upload can be added to a session but cannot be linked to a message without the credential.

Client audit (all on `origin/main`): luran-chat `useChat.ts` sends `user_identifier` on both `/chat/message` call sites (widget and hosted page use the visitor token; the authed area uses the customer endpoint). luran-dashboard and luran_agent only call staff-authenticated session routes. luran-widget is an iframe loader and calls none of these.

## Files
`app/api/v1/endpoints/chat.py`, `chat_video.py`, `voice.py`, `app/schemas/voice.py`, `app/services/review_service.py`, `customer_conversations.py`, `app/utils/customer_credential.py` (new), `app/utils/staff_redaction.py`, `app/core/config.py`. No migration.

## Verification
- `tests/api/v1/test_customer_credential_hardening.py` (18): bare session id, staff token, wrong/right identifier, visitor token, metadata binding and reuse, `/chat/sessions`, authed endpoint still binds, upload/video wrong/missing/enforced, owning customer vs staff vs stranger token. Exploit tests were red before the fix.
- `tests/rating/test_staff_session_meta_allowlist.py` (7): allow-list unit, session detail/list, `GET /chat/sessions/{id}/messages`, platform `GET /platform/reviews/ratings/all`, authed-customer socket fan-out to agents. The allow-list tests fail against the old block-list.
- Existing tests that posted `session_id` only now send `user_identifier`.
- Full suite on Postgres 15 + Redis 7 (CI env): baseline 1658 passed / 33 skipped; after 1690 passed / 33 skipped. ruff 0.8.6 and black 24.10.0 clean on `app/`.
- Live run and captures: `docs/captures/customer-credential-hardening/` (migrated Postgres, uvicorn, curl).

## Operate / roll back
Revert the commit; no data changes. Flip `CHAT_MEDIA_REQUIRE_CUSTOMER_CREDENTIAL=true` only after luran-chat sends `user_identifier` on the three media calls.

## Follow-ups
- luran-chat: send `user_identifier` on `uploadChatImage`, `signVideo`, `registerVideo`, then enable the flag.
- Message meta redaction is still a block-list (`rating_token`).
- `customer_name` / `customer_email` in client metadata are still trusted for display and contact capture.
- luran-chat: add `user_identifier` to `mintVoiceToken`.
