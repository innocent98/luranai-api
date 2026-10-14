# Voice token mint: customer credential (2026-10-14)

`POST /api/v1/voice/session/token` now requires the customer credential for `luran_session_id`. Captures: `docs/captures/customer-credential-hardening/18`-`20`.

## Request change
Add ONE of these to the existing body (`tenant_id`, `luran_session_id`, `text_out`):

| Field | Value |
|---|---|
| `user_identifier` | the same value luran-chat sends as `user_identifier` on `/chat/message` (visitor token for widget/hosted page; `chat_user_key` for the authed area) |
| `visitor_token` | the visitor's public token (alternative) |

**Authed customers need no explicit field:** if the request carries the customer access token (cookie, so the fetch needs `credentials: 'include'`, or a Bearer header) and that customer owns the session (`customer_user_id` or `chat_user_key` match), the mint is accepted. A signed-in customer who does not own the session, with no explicit credential, gets the 403 below. Verified by pytest only (`tests/voice/test_token_mint.py`), not captured live: the current `mintVoiceToken` fetch has no `credentials: 'include'`, so the cookie is not sent until luran-chat adds it.

luran-chat today (`app/lib/voice-api.ts` `mintVoiceToken`) sends neither, so every mint now gets 403 until it is updated: pass `userIdentifier` through `useVoiceSession` into `mintVoiceToken` and add `user_identifier` to the JSON body. Voice is granted to no production tenant, so nothing is live-affected.

## New error (verified live, 18/19)
```
HTTP 403  {"detail": {"reason": "invalid_customer_credential"}}
```
Same `detail.reason` shape the client already parses; the current mapper turns an unknown reason into a generic fall-back-to-text, which is the right UX. The body never echoes the credential.

## Happy path
With a correct credential the request proceeds to the existing gating (live `20`: this seed tenant has voice off, so `409 {"detail": {"reason": "voice_disabled"}}`). The full 200 mint body was NOT captured live: it needs a real ElevenLabs call. Its shape is unchanged and covered by `tests/voice/test_token_mint.py`.
